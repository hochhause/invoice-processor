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
- Each column shows jobs; column header shows running amount sub-totals **grouped by the debtor account each block debits** (T13): e.g. BKB renders `CHF acct ← CHF … · SEK … ⇄` and `EUR acct ← EUR …`, where `⇄` flags an FX block (payment ccy ≠ account ccy, e.g. SEK debiting the CHF account). Resolution is config-driven via `GET /api/accounts-summary` — no hardcoded map, same `config.resolve_account` as routing/`build_pain001`.
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

**Export readiness gate (T9 done):** Three hard-block rules — all enforced by `_export_blockers()` in `app/main.py`:
1. **Incomplete** — status `{needs_review, error, LLM-Pending}` or missing mandatory field (`receiver`, `iban`, `amount`, `currency`).
2. **Unresolvable debtor account** — job is routed (`BKB`/`RAIFFEISEN`) but `config.resolve_account(bank, ccy)` returns no configured IBAN+BIC (including default fallback).
3. **Cross-border gap** — `RAIFFEISEN` job missing creditor `bic` or `cdtr_country` (required for SWIFT).

Cards matching rule 2 or 3 are **greyed + undraggable** on the export board (`export.js: blockedIds`), identical visual treatment to `needs_review` but with a different tooltip. `blockedIds` is refreshed after each drag+drop (reassignment can change resolvability). `MANUAL` cards remain draggable (unknown-ccy → drag into bank → resolves to default account via T1). See [[DECISIONS#Export Hard-Blocked on Incomplete Invoices]], [[Plan#T9 — Export gating]].

**APIs:**
- `GET /api/jobs` (load jobs)
- `GET /api/export-readiness` (`{ready, blockers[]}` — gate the button + drive popup)
- `GET /api/accounts-summary` (per-bank `ccy → account-ccy` map — drives per-account sub-totals; T13)
- `POST /api/assign-bank/{id}` (reassign bank)
- `POST /download/confirm` (409 + `blockers[]` if not ready; else generate, archive, zip)

---

## 6. pain.001 XML Export (per bank, per account)

**Implemented in:** Step 5 (`app/xml_export.py`), Step 4 (`app/main.py`)

**Output:** Two separate pain.001 files
- `pain001_BKB.xml` — for BKB jobs (CHF NURG, SEK NURG+SWIFT, EUR SEPA)
- `pain001_Raiffeisen.xml` — for Raiffeisen jobs (USD/CAD/GBP NURG+SWIFT)

**Behavior:**
- Filters jobs by `bank_target` parameter
- Groups PmtInf blocks by currency (ISO 20022 requirement)
- Applies correct service level codes per bank + currency
- Raises HTTP 400 if no jobs match filter (no empty files)

**In-progress rework (export-rework plan — see [[Plan]]):**
- ✅ Upgrade schema `pain.001.001.03` → `pain.001.001.09` (SIX CH XSD) — **T3 done**: namespace/schemaLocation, `BIC`→`BICFI` (Dbtr+Cdtr agents), `ReqdExctnDt` wraps `<Dt>`; output XSD-validated in startup tests (guarded `Txsd-*`, active once `xmlschema` ships in T10)
- ✅ Per-account `DbtrAcct`/`DbtrAgt` per currency block — **T4 done**: `build_pain001(jobs, accounts, bank)` resolves each ccy PmtInf via `config.resolve_account(bank, ccy)`; `DbtrAcct/Ccy` = *account* currency (SEK→CHF fallback debits BKB-CHF IBAN with `Ccy=CHF`), per-tx `InstdAmt/Ccy` = payment currency (FX); `DbtrAgt/BICFI` from the resolved account (no more empty `<BICFI/>`). SEK+EUR output XSD-validated vs SIX CH XSD. See [[DECISIONS#Per-Account Debtor Model]].
- ✅ Cross-border conformance — **T5 done**: `ChrgBr` at PmtInf level (SEPA→`SLEV`, SWIFT→`SHAR`, CHF domestic omitted); `_cdtr_address` emits structured `Cdtr/PstlAdr` (`StrtNm`,`BldgNb`,`PstCd`,`TwnNm`,`Ctry`) when `cdtr_*` fields present (silent no-op until T7 persists them). Output remains XSD-valid. See [[DECISIONS#pain.001.001.09 Migration]].
- ✅ LLM extracts creditor address — **T6 done**: `PROMPT` in `app/llm.py` extended with 5 new keys (`cdtr_street`, `cdtr_building_no`, `cdtr_postcode`, `cdtr_town`, `cdtr_country` ISO-2); all null-safe; fields flow through pipeline but are dropped by `PERSIST_KEYS` until T7 adds DB columns.
- ✅ DB columns + migration — **T7 done**: `cdtr_street/building_no/postcode/town/country` added to `jobs` table (`SCHEMA`, `_JOB_COLUMNS`, ALTER TABLE migrations in `init_db`). `PERSIST_KEYS` + `REVIEW_FIELDS` in `main.py` updated — LLM address output now persists; operators can manually correct via review form. Existing rows default to `''` (no PstlAdr emitted until re-extracted or edited). See [[DECISIONS#pain.001.001.09 Migration]].
- Accounts loaded from `.env` via `app/config.py` → `resolve_account(bank, ccy)`
- ✅ Route wiring, export gating, deps/XSD, tests — **T8–T11 done** (`_accounts()` in download route; three-rule blocker gate; `xmlschema` + SIX CH XSD; full startup assertion suite incl. `Tdelta`/`Tfx`/`Txsd`)
- ✅ Trailing docs/config — **T12** (`.env.example` cleaned: per-bank keys, no `DEBTOR_IBAN`/`DEBTOR_BIC`), **T13** (per-account export sub-totals via `/api/accounts-summary`), **T14** (this doc sync). **T0–T14 complete.**

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
