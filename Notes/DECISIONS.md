# Invoice Processor — Design Decisions

---

## Architecture: LLM-First (feat/llm-first)

### QR-on-Upload, LLM-on-Demand

**Decision:** QR scan runs synchronously on every upload. LLM extraction is manually triggered (batch, month-end). No automatic LLM on upload.

**Reason:** QR is instant and free. LLM costs money. Operators collect the full monthly batch first, then run LLM once to minimize API calls. Forcing LLM on every upload would trigger costs before the batch is complete.

**Implementation:**
```
upload → qr_swiss.extract_from_pdf() → status=QR-processed|LLM-Pending
manual trigger → POST /api/run-llm-batch → status=LLM-Done|needs_review
```

---

### QR Invoices Skip LLM (with exceptions)

**Decision:** Jobs with `status=QR-processed` are excluded from LLM batch, except when:
- `bic` is empty AND `currency != CHF` (CHF domestic pain.001 doesn't require BIC)
- `due_date` is empty

**Reason:** Swiss QR (SPC) does not encode BIC or due_date. For CHF payments, Swiss bank gateways derive BIC from IBAN — so BIC can be omitted. For non-CHF QR invoices (rare), BIC is required and LLM must supply it.

**QR fields provided:** `iban`, `receiver`, `amount`, `currency`, `reference`
**QR fields missing:** `bic`, `due_date`, `invoice_id`

---

### LLM Returns Structured JSON (not raw OCR text)

**Decision:** LLM prompt instructs Claude Haiku to return a JSON object directly. No regex post-processing on LLM output.

**Reason:** Prior approach: LLM → raw text → regex → fields. This created a two-failure-mode system (LLM wrong OR regex wrong). Single-step structured extraction is simpler and more reliable.

**Prompt contract (updated T6):**
```
Return ONLY valid JSON with keys: invoice_id, receiver, amount (decimal string),
currency (ISO 3-letter), due_date (YYYY-MM-DD), iban, bic, reference,
cdtr_street, cdtr_building_no, cdtr_postcode, cdtr_town,
cdtr_country (ISO 3166-1 alpha-2 or null).
Use null for missing fields. No markdown.
```
Address fields (`cdtr_*`) refer to the receiver's mailing address as printed on the
invoice. `cdtr_country` must be a 2-letter ISO code or null — never a full name.
All 13 keys are always present; unknown fields are null, not omitted.
See [[Plan#T6 — LLM extracts creditor address (structured)]].

---

### Claude Haiku Only (no Ollama/DeepSeek)

**Decision:** Remove Ollama and DeepSeek providers. Use Claude Haiku exclusively.

**Reason:** Ollama (local) was unreliable on scanned PDFs. DeepSeek added provider complexity with no clear benefit. Haiku is cheap (~$0.25/MTok input), fast, and reliable for structured extraction.

**Cost estimate (monthly batch of 100 invoices):** ~$0.50–$1.00 total.

---

### DB Schema: pain.001 Fields Only

**Decision:** Remove all non-payment fields from the `jobs` table. Remove `vendors` table entirely (to be redesigned later).

**Removed:** `bankgiro`, `plusgiro`, `review_reasons`, `flags`, `field_statuses`, `error_msg`, `ocr_method`, `vendors` table

**Kept:**
```sql
id, filename, status, receiver, iban, bic, amount, currency,
due_date, reference, invoice_id, bank_target, created_at, updated_at
```

**Status enum:**
- `QR-processed` — QR scan succeeded, fields extracted
- `LLM-Pending` — awaiting LLM batch processing
- `LLM-Done` — LLM extracted all mandatory fields (receiver, iban, amount, currency)
- `needs_review` — extraction (QR or LLM) missing mandatory fields, human review required
- `archived` — processed & exported, hidden from main view but retained for audit
- `error` — extraction failure

**Reason:** The prior schema tracked extraction metadata (field confidence, flag lists) that only made sense with the regex pipeline. The LLM-first pipeline has a simpler trust model: LLM returns fields or it doesn't. `archived` is a logical flag (hidden frontend) but stored as status in DB for simplicity.

---

### Bank Routing via `bank_target` Column

**Decision:** `bank_target` is auto-set on extraction completion. Operator can override via export screen drag or edit modal.

| Currency | Default target |
|----------|---------------|
| CHF, SEK, EUR | BKB |
| USD, CAD, GBP | Raiffeisen |
| other | MANUAL (unsorted) |

**Reason:** BKB handles CHF domestic + SEPA-adjacent (SEK, EUR). Raiffeisen handles international. Unknown currencies require human decision — not assumed.

---

### Export Screen as Separate Route

**Decision:** Export is a distinct full-screen view (`GET /export`, `app/templates/export.html`), not a modal on the main dashboard.

**Reason:** The three-column drag-board requires significant screen real estate. Embedding it in the main table view would require complex layout switching. A dedicated route is cleaner and allows the export screen to have its own state (drag positions, confirmation flow).

---

### Archived Status Instead of Delete

**Decision:** After successful download, sorted invoices are flagged `status=archived`. PDFs remain on disk. They do not appear in the main table or export screen.

**Reason:** Audit trail. Pain.001 files reference invoices; if a question arises later (bank rejects a payment), the original PDF must be traceable. Hard delete after export would lose this.

**Access:** Archived jobs excluded from `/api/jobs` default query. Can be retrieved via `/api/jobs?include_archived=true` for audit.

---

### Unsorted Invoices Excluded from Download (Not Blocked)

**Decision:** Unsorted invoices (`bank_target=MANUAL`) are silently excluded from pain.001 generation. Operator sees a warning count and must confirm before download proceeds.

**Reason:** Blocking the whole download if any invoice is unsorted would frustrate operators. Excluding with confirmation gives flexibility while surfacing the gap.

---

### Export Hard-Blocked on Incomplete Invoices

**Decision:** `POST /download/confirm` refuses (HTTP 409) if any non-archived invoice still has `status ∈ {needs_review, error, LLM-Pending}` or is missing a mandatory pain.001 field (`receiver`, `iban`, `amount`, `currency`). The response carries a `blockers` list (`id`, `filename`, `status`, `missing[]`) and the export screen renders a popup listing them; the operator must fix all before export proceeds. `GET /api/export-readiness` exposes the same check so the UI can pre-gate the button.

**Distinction from the MANUAL rule above:** unrouted-but-complete invoices (`bank_target=MANUAL`) are *excluded with confirmation* — they do not block. Incomplete / review-pending invoices *hard-block* — bad data must not silently drop out of a payment file. Two different failure modes, two different responses.

**Reason:** A pain.001 file is a payment instruction. Silently omitting an invoice that an operator believes is being paid is a financial-correctness hazard; forcing completion first guarantees the exported batch == the operator's intent.

**Location:** `app/main.py` → `_export_blockers()`, `download_confirm()`, `export_readiness()`.

**T9 — Export gating extended (done):** `_export_blockers()` now runs three checks (rule 1 is the original hard-block; rules 2–3 are new):
- Rule 1 (`blocker_type: incomplete`) — unchanged: bad status or missing mandatory field.
- Rule 2 (`blocker_type: unresolvable_account`) — job routed to BKB/RAIFFEISEN but `config.resolve_account(bank, ccy, accounts)` returns no account (IBAN or BIC absent, including default fallback). Prevents `build_pain001` from raising `ValueError` at download time.
- Rule 3 (`blocker_type: cross_border_incomplete`) — RAIFFEISEN job missing creditor `bic` or `cdtr_country`; required for valid SWIFT output (empty `<BICFI/>` or missing `<Ctry>` → bank silent reject).

Each blocker entry now carries `blocker_type` (backwards-compat — callers that ignore unknown fields are unaffected). Frontend (`export.js`) fetches `/api/export-readiness` on init **and** after each drag+drop; builds `blockedIds` Set; cards matching rules 2–3 are greyed + undraggable (same CSS as `needs_review` + distinct tooltip). Unknown-ccy `MANUAL` cards stay draggable → drag into bank → T1 `resolve_account` picks the default account. See [[Plan#T9 — Export gating]], [[Features#5. Export Screen — Bank Assignment & Download]].

---

### LLM Input Strategy: Images Only (for now)

**Decision:** LLM extraction uses PDF page images only (converted at 150 DPI). Text layers are not extracted or sent to the model.

**Rationale — three approaches considered:**

| Approach | Cost/invoice | Accuracy | Tradeoff |
|---|---|---|---|
| **Text-first (fallback)** | 0.0024¢ best, 0.050¢ worst | ⚠️ Silent failures on corrupt text | Too risky; text layers can be stale/corrupted |
| **Text + images (both)** | 0.050¢ (constant) | ⭐⭐⭐⭐⭐ Robust | Always expensive; overkill for clean PDFs |
| **Image-only (chosen)** | 0.048¢ | ⭐⭐⭐⭐ Good | Works for all PDF types; proven reliable |
| Hybrid (future) | 0.026¢ (avg) | ⭐⭐⭐⭐⭐ | Best long-term if text layer adoption is high; requires conditional logic |

**Current implementation:** Images at 150 DPI are sufficient for invoices. OCR confusion (e.g., `I`/`1`, `O`/`0`) is a model quality issue, not an input strategy issue — would persist with text extraction too.

**Future consideration:** If 80%+ of invoices have clean embedded OCR text layers, switch to **hybrid** (text-first with image validation fallback) to cut costs by ~45%. Requires tracking text layer quality to make the decision.

**Location:** `app/llm.py` → `_pdf_to_images()`.

---

### HTTP Basic Authentication (optional, env-gated)

**Decision:** Protect the whole app with HTTP Basic auth, enforced as an app-wide
`Depends(require_auth)` on the FastAPI instance. A single shared password lives in
`APP_PASSWORD` (username `APP_USERNAME`, default `admin`). When `APP_PASSWORD` is
unset/empty, auth is **disabled** (a startup warning is logged); set it only on the
deployed instance. `/static/*` is left open (mounted sub-app).

**Reason:** "Simple password protection for secure deployment." HTTP Basic was chosen
over a login-page + cookie session after weighing both: Basic is ~15 lines, zero new
dependencies, browser-native, and uniformly protects pages + the fetch-driven API +
the PDF iframe. Trade-off accepted: no styled login page and no clean logout (the
browser caches credentials until all tabs close). HTTPS is assumed in deployment
(Basic creds are re-sent every request). `secrets.compare_digest` guards timing.

**Auth-off-when-unset** keeps local dev frictionless; fail-closed was rejected to
avoid forcing the env var on every local run.

**Location:** `app/auth.py`, wired in `app/main.py` (`FastAPI(dependencies=[...])`).

---

### Cost Tracking from Real Token Usage (model-aware)

**Decision:** Capture `usage.input_tokens` / `usage.output_tokens` from every Claude
API response and persist them per job (`input_tokens`, `output_tokens`, `llm_model`
columns). `/api/analytics` sums tokens grouped by model and prices them via
`cost.estimate_cost()`; the dashboard analytics modal shows total USD + a per-model
breakdown. QR-only jobs incur no tokens (stay at 0).

**Reason:** "As close as feasible" → use the tokens the API actually billed, not a
char-count estimate. Storing tokens + model (rather than a precomputed cost) makes the
estimate **model-aware**: switching `LLM_MODEL` to Sonnet reprices new jobs
automatically, and the price table in `cost.py` is the single place to update rates.
Usage is captured even when JSON parsing fails (tokens were still spent). Prompt
caching is not enabled, so cache tokens are 0 and not separated.

**Pricing (USD per 1M tok, input/output):** Haiku 4.5 $1/$5 · Sonnet 4.5/4.6 $3/$15 ·
Opus 4.8 $5/$25. Unknown models fall back to Haiku rates.

**Location:** `app/llm.py` (usage capture), `app/pipeline.py` (`_sum_usage`),
`app/cost.py` (pricing), `app/main.py` (`/api/analytics`), `index.html` (modal).

---

## Retained Decisions (from prior architecture)

### IBAN MOD-97 Checksum Validation

**Decision:** Manual MOD-97 validation retained. Applied to LLM-returned IBAN values.

**Reason:** LLM can hallucinate plausible-looking IBANs. Checksum catches this early.

---

### ISO 20022 pain.001 Multi-Currency Grouping

**Decision:** One PmtInf block per currency within each bank file.

**Location:** `app/xml_export.py`

**Reason:** Bank gateways require separate payment info blocks per currency for correct routing.

---

### SQLite with Named Docker Volume

**Decision:** SQLite retained. Volume-mounted to avoid OneDrive lock conflicts.

**Reason:** No change in deployment model. Simplicity wins over PostgreSQL for this use case.

---

### Dev/Prod Mode Split

**Decision:** DEV_MODE retained. Relaxes IBAN validation. Startup tests adjusted to cover new LLM-first pipeline.

**Removed tests:** T3 (regex extraction structure) — no regex pipeline.
**Added tests:** T7 (bank_target routing logic), T8 (LLM JSON validation).

---

## Export Rework — Per-Account + pain.001.001.09 (PLANNED)

> Full task breakdown + file:line refs: [[Plan]].

### Per-Account Debtor Model (.env-driven)

**Decision:** Replace the single global debtor account (`DEBTOR_IBAN`/`DEBTOR_BIC`) with a per-bank, per-currency account set defined entirely in `.env`. Each bank declares its currencies, a default account, and one account per currency:

```
DEBTOR_NAME=Lyfegen HealthTech AG
BKB_CURRENCIES=CHF,EUR,SEK        BKB_DEFAULT_CCY=CHF
BKB_CHF_IBAN/BIC   BKB_EUR_IBAN/BIC
RAIFFEISEN_CURRENCIES=USD,CAD,GBP RAIFFEISEN_DEFAULT_CCY=USD
RAIFFEISEN_{USD,CAD,GBP}_IBAN/BIC
```

**Resolution:** `bank = bank whose *_CURRENCIES holds ccy (else MANUAL)`; `acct = accounts[bank].get(ccy) or accounts[bank][DEFAULT_CCY]`.

**Reason:** Adding/changing an account must be a config change, not a code change. The default-account fallback satisfies the operator rule: SEK (and any other BKB-routed ccy without its own account) debits the **BKB CHF** account; Raiffeisen's fallback is **USD**. Routing (`derive_bank_target`) is derived from the same map — one source of truth, no drift between routing and accounts.

**Debugging notes:**
- A currency PmtInf draws DbtrAcct/DbtrAgt from the *resolved* account, so SEK and CHF blocks can legitimately reference the **same** IBAN. Not a bug.
- PmtInf is grouped per **transaction currency** (keeps `CtrlSum` single-currency-correct). `DbtrAcct/Ccy` = account currency; per-tx `InstdAmt/Ccy` = payment currency — an FX payment when they differ.
- Unknown currency → `MANUAL`; operator may drag it into a bank column → it then resolves to that bank's default account.

**Location:** `app/config.py` (**T1 done** — `load_accounts`, `get_accounts`, `resolve_account`, `_clear_cache`; **T4** extended `resolve_account(bank, ccy, cfg=None)` to also return the resolved *account* `ccy` and to resolve against an explicitly-passed config, keeping `build_pain001` pure — one source of truth, no drift); `app/db.py:derive_bank_target` (**T2 done** — config-driven via `ccy_bank_index`); `app/xml_export.py:build_pain001` (**T4 done** — signature `build_pain001(jobs, accounts, bank)`; each ccy PmtInf draws `DbtrAcct`/`DbtrAgt` from `resolve_account(bank, ccy)`, `DbtrAcct/Ccy`=account ccy, per-tx `InstdAmt/Ccy`=payment ccy; raises `ValueError` if a payable (bank,ccy) has no resolvable IBAN+BIC — defensive, T9 gates upstream); `app/main.py:_accounts` (**T8 done** — `_debtor()` replaced by `_accounts()` = `config.get_accounts()`; download route passes the full accounts dict to both `build_pain001` calls; export path is fully wired); `app/main.py:accounts_summary` + `app/static/js/export.js` (**T13 done** — read-only `GET /api/accounts-summary` returns `{BANK:{default_ccy, resolve:{ccy:acct_ccy}}}` from the **same** `config.resolve_account`, so the export board can show *which* debtor account each currency block debits — SEK under "CHF acct ⇄" (FX), EUR under "EUR acct" — with no hardcoded map and no drift from routing/`build_pain001`).

**T12 — `.env.example` (done, operator-applied):** the per-account keys (`DEBTOR_NAME`, `{BANK}_CURRENCIES`, `{BANK}_DEFAULT_CCY`, `{BANK}_{CCY}_IBAN/BIC`) replace the single global `DEBTOR_IBAN`/`DEBTOR_BIC`, which are dropped. `.env.example` lives in a sandbox-denied path → the full body is delivered to the operator for paste; [[PROJECT_CONTEXT#Environment Variables]] is the mirrored source of truth.

---

### pain.001.001.09 Migration

**Decision:** Upgrade the generator from `pain.001.001.03` to `pain.001.001.09` and validate output against the XSD in tests.

**Three breaking deltas (silent bank-reject if missed) — keep front-of-mind when debugging "bank rejected the file":**
1. Namespace/schemaLocation → `urn:iso:std:iso:20022:tech:xsd:pain.001.001.09`.
2. Financial-institution id element renamed `BIC` → **`BICFI`** (both `DbtrAgt` and `CdtrAgt`).
3. `ReqdExctnDt` is no longer a bare date — it wraps a choice: `<ReqdExctnDt><Dt>YYYY-MM-DD</Dt></ReqdExctnDt>`.

**Cross-border conformance (full, T5 done):** **pure-structured** creditor address (`Cdtr/PstlAdr`: `StrtNm`,`BldgNb`,`PstCd`,`TwnNm`,`Ctry`) — CH design: **no `AdrLine`** (even though `PostalAddress24_pain001_ch_3` technically allows `AdrLine` with `minOccurs=0`, structured fields are required by Swiss payments practice); `ChrgBr` at PmtInf level: SEPA→`SLEV`, SWIFT→`SHAR`, CHF domestic omitted; enforced creditor `BICFI`. New `cdtr_*` columns + structured LLM extraction supply the address (T6/T7 pending); pre-migration jobs lack it → `_cdtr_address` emits nothing silently → expect `needs_review` for cross-border until T6/T7 land.

**Reason:** Newer Swiss Payment Standards (SIX) ride on `.09` with structured addresses; `.03` is being retired. Validating against the XSD in CI turns "bank rejects on upload" into a failing local test.

**XSD in hand:** `app/schemas/pain.001.001.09.ch.03.xsd` — the **SIX Swiss-restricted** schema (©2021), not the generic ISO one. Its `targetNamespace` is the plain ISO `urn:iso:std:iso:20022:tech:xsd:pain.001.001.09` (root type `Document_pain001_ch`), so our output namespace is unchanged; only `xsi:schemaLocation` filename points at it.

**Residual risk:** the XSD validates **structure/types, not presence** (address fields are all `minOccurs=0`) and enforces a restricted **char-set** (Latin subset). Missing country/BIC for cross-border and non-Latin chars must be caught by our own export blockers + the SIX **Validation Portal** (validation.iso-payments.ch), not the XSD alone.

**Location:** `app/xml_export.py` (**T3 done** — namespace/schemaLocation → `.09.ch.03`, `BIC`→`BICFI` on Dbtr+Cdtr agents, `ReqdExctnDt` now wraps `<Dt>`; **T5 done** — `_cdtr_address` helper emits structured `PstlAdr`, `ChrgBr` at PmtInf level), `app/schemas/pain.001.001.09.ch.03.xsd` (present), `app/tests.py` (**T3 done** — ns strings bumped `.03→.09`; **T5 done** — T5b–T5f assert ChrgBr + PstlAdr rules, Txsd validates; full T11 assertion suite still pending).

**T3 residual (financial-correctness) — CLOSED by T4:** the `.03→.09` rename was structural only and an empty debtor BIC emitted an invalid `<BICFI/>`. **T4** now sources `DbtrAgt/BICFI` (and `DbtrAcct/IBAN`) from the resolved per-(bank,ccy) account, and `build_pain001` raises `ValueError` if that account lacks IBAN+BIC — so the generator can no longer emit an empty `<BICFI/>`. T9 (export gating) still owns surfacing unresolvable accounts to the operator *before* download. See [[Plan#T4 — Per-account DbtrAcct/DbtrAgt]].

**T7 — DB persistence (done):** `cdtr_street`, `cdtr_building_no`, `cdtr_postcode`, `cdtr_town`, `cdtr_country` columns added to the `jobs` table (`db.py:SCHEMA`, `_JOB_COLUMNS`, ALTER TABLE migrations). `PERSIST_KEYS` and `REVIEW_FIELDS` in `main.py` updated — pipeline writes address fields from LLM output; operators can correct them via the review form. Existing rows default to `''`; pre-migration cross-border jobs will lack address data until re-extracted or manually filled. See [[Plan#T7 — DB columns + migration]].

---

### Vendor IBAN Takes Priority (space-insensitive)

**Decision:** When a receiver matches the vendor table, the **vendor-table IBAN is authoritative** and overrides the LLM/document-read IBAN. IBAN comparison ignores structural fillers (spaces) — both sides normalized (`[^A-Za-z0-9]` stripped, upper-cased) before compare. LLM always emits IBANs spaceless; the **frontend and vendor table display** them grouped in 4s for readability, while storage stays normalized.

**Reason:** The curated vendor table is the trusted record; per-invoice OCR/LLM extraction is the lower-trust source. Space differences were producing false `document_mismatch` flags. On a real mismatch the table IBAN wins, the rejected extracted value is preserved in `iban_mismatch_db`, and the `doc ⚠` chip surfaces it for operator awareness (status unchanged — non-blocking).

**Debugging notes:**
- `iban_source=document_mismatch` now means "LLM IBAN was overridden by the table," not "we kept the LLM IBAN." `iban` holds the table value; `iban_mismatch_db` holds the discarded extracted value.
- R if the vendor table is stale, a legitimately-new invoice IBAN is overridden → operator must update the vendor record (the `↑ Update vendor IBAN` action). This trade trusts curation over extraction by design.

**Location (planned):** `app/pipeline.py:run_vendor_check`, `app/vendors.py` (normalize on store), `app/static/js/{dashboard,modal,vendors}.js` (display formatting).

---

## Test Suite (T11 DONE — Opus verify pass 2026-06-10)

**XSD Validation (pain.001.001.09):** Startup self-tests (DEV_MODE=true) validate the `.09` output against the SIX CH XSD (`app/schemas/pain.001.001.09.ch.03.xsd`). `Txsd` confirms CHF domestic and multi-currency (EUR) output is valid. Guarded import: xmlschema is optional (lands with T10 via requirements.txt); skips gracefully if absent.

**Direct delta + FX assertions (added in verify pass):** the XSD catches the `.09` deltas only *indirectly*. Two assertion blocks now pin them on the emitted XML so a regression names itself instead of surfacing as a generic "XSD invalid":
- `Tdelta-a..f` — namespace urn, `BICFI` on **both** DbtrAgt + CdtrAgt agents, **no legacy `<BIC>`** element survives, and `ReqdExctnDt` wraps `<Dt>` (not a bare date).
- `Tfx-a..f` — the per-account FX mechanic: in a BKB file, CHF + SEK PmtInf blocks both debit the **BKB-CHF IBAN** (SEK falls back to default), EUR debits its **own BKB-EUR IBAN**, and the SEK block carries `DbtrAcct/Ccy=CHF` (account ccy) vs `InstdAmt/@Ccy=SEK` (payment ccy) — confirming FX is represented correctly.

All checks pass when env config is set (see [[Plan#T11 — Tests incl. XSD validation]]). Prior Haiku done-note had cited line numbers for these assertions that did not in fact exist (they pointed at ChrgBr/config-resolver tests); corrected here.

---

## Desktop Packaging — PyInstaller onedir + app-data (branch: desktop, 2026-06-10)

**Decision:** Ship the app to non-technical users as a PyInstaller `--onedir` bundle (zipped folder, double-click `InvoiceProcessor.exe`) rather than Docker Desktop or an install script.

**Why:**
- Target user installs nothing: unzip → double-click → browser opens. Docker Desktop is the hassle we're avoiding (install, keep running, commercial licensing); a uv/script install leaves moving parts and needs a terminal.
- `--onedir` over `--onefile`: starts much faster (no self-extraction per launch); zip the folder.

**Key choices (all in `desktop/` + `app/paths.py` / `app/settings_store.py`):**
1. **Mode detection, not forked code** — `paths.is_desktop()` (PyInstaller `sys.frozen` or `INVOICE_DESKTOP=1`). Container behaviour is byte-identical: `/app/data` defaults preserved, env vars `DB_PATH`/`UPLOAD_DIR` always win.
2. **Writable data in per-user app-data** (`%APPDATA%\InvoiceProcessor`, `~/Library/Application Support/InvoiceProcessor`) — a bundle may run from a read-only location, and replacing the app folder on update must never delete `invoices.db`.
3. **API key via first-run web UI**, persisted to `<app-data>/settings.env` — never baked into the binary, no terminal needed. `POST /api/settings/api-key` is 403 outside desktop mode (server stays env-managed). llm.py reads the key per call → applies without restart.
4. **settings.env over JSON** — same KEY=VALUE vocabulary as the server `.env`; `settings_store.load_into_environ()` runs before app imports, so all existing env-driven config (bank accounts, models) works unchanged. Real env vars take precedence (`setdefault`).
5. **pyzbar optional, zxing-cpp primary in the bundle** — pyzbar needs the system zbar shared library which PyInstaller won't collect; `qr_swiss.py` degrades gracefully (`zbar_decode=None`), zxing-cpp ships its binary in the wheel. Docker keeps both (libzbar0 in image).
6. **Console window = quit affordance** (`console=True`) — visible logs, "close the black window to stop" is explainable to anyone; no tray-icon dependency (pystray) for v1.
7. **Builds on GitHub Actions** (`desktop-build.yml`, manual or `desktop-v*` tag) — PyInstaller cannot cross-compile; windows-latest + macos-latest jobs, each smoke-tests the built binary (boots server, probes `/api/settings/status`) before uploading the zip.

**Accepted trade-offs:**
- R unsigned binaries → SmartScreen "More info → Run anyway" / Gatekeeper right-click-Open, once per machine. Documented in `desktop/README.md`.
- R updates are manual (rebuild + send zip). If frequent → add a version-check banner endpoint later.
- R `settings.env` stores the API key in plain text in app-data — same trust level as a server `.env`; deliberate, no fake obfuscation.
- N `DEV_MODE=true` in a desktop build crashes at startup: `tests` module is excluded from the bundle. Template ships it commented out.

**Location:** `desktop/{launcher.py, InvoiceProcessor.spec, settings.env.template, requirements-desktop.txt, README.md}`, `app/{paths.py, settings_store.py}`, settings routes in `app/main.py`, `app/static/js/settings.js`, `.github/workflows/desktop-build.yml`. See [[Features#12. Desktop App Packaging (branch: desktop)]].

---

## Notable TODOs / Gaps

**[MISSING]** Rate limiting on `/api/run-llm-batch` — could accidentally trigger multiple concurrent LLM batches if clicked twice. Add a lock flag in DB (`llm_batch_running`).

**[MISSING]** LLM response validation — if Haiku returns malformed JSON, need graceful fallback to `needs_review` status rather than crash.

**[DONE]** Authentication — optional HTTP Basic via `APP_PASSWORD` (see "HTTP Basic Authentication" above). Single shared password; disabled when the env var is unset.

**[FUTURE]** Archived PDF viewer — accessing archived invoices for audit currently requires direct DB query.
