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

**Prompt contract:**
```
Return ONLY valid JSON with keys: invoice_id, receiver, amount (decimal string),
currency (ISO 3-letter), due_date (YYYY-MM-DD), iban, bic, reference.
Use null for missing fields. No markdown.
```

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

## Notable TODOs / Gaps

**[MISSING]** Rate limiting on `/api/run-llm-batch` — could accidentally trigger multiple concurrent LLM batches if clicked twice. Add a lock flag in DB (`llm_batch_running`).

**[MISSING]** LLM response validation — if Haiku returns malformed JSON, need graceful fallback to `needs_review` status rather than crash.

**[MISSING]** Authentication — no user login. Assumes internal network.

**[FUTURE]** Archived PDF viewer — accessing archived invoices for audit currently requires direct DB query.
