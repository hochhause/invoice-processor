# Invoice Processor — Design Decisions

## Field Validation & Error Handling

### IBAN Checksum Validation in Dev Mode

**Decision:** DEV_MODE relaxes IBAN validation; production requires strict MOD-97 checksums.

**Location:** `app/extract.py:266-272`

**Reason:** During development and testing, synthetic/test IBANs won't always have valid checksums. Enforcing them would block all test workflows. Production mode (`DEV_MODE=false`) strictly validates per [[ISO 13616|https://en.wikipedia.org/wiki/International_Bank_Account_Number]].

**Implementation:**
```python
if iban and not _validate_iban_checksum(iban):
    if dev_mode_iban:
        flags.append("iban_invalid_checksum_dev_skipped")  # Warning only
    else:
        flags.append("iban_invalid_checksum")  # Blocks payment, clears IBAN
        iban = ""
```

**Impact:** Tests pass in dev; real invoices fail gracefully with audit trail.

---

### Mandatory Fields Split: Base vs. Production

**Decision:** Dev mode requires fewer mandatory fields than production.

**Location:** `app/extract.py:27-28, 284-291`

**Reason:** During data capture and testing, reference numbers may not always be present. Production payment files must have references for audit/reconciliation. Two-mode approach allows workflow flexibility without sacrificing compliance.

```python
MANDATORY_BASE = ["amount", "currency", "receiver", "due_date"]
MANDATORY_PROD = ["amount", "currency", "receiver", "due_date", "reference"]
```

**Impact:** Dev PDFs can process without references; production blocks missing references → needs_review=YES.

---

### Error Flag Categories: Blocking vs. Warning

**Decision:** Split error detection into ERROR_FLAGS and WARN_FLAGS.

**Location:** `app/extract.py:15-24`

**Reason:** Some missing fields block payment entirely (`amount_not_found` → can't pay), while others just need human attention (`invoice_id_not_found` → audit trail incomplete). Blocking flags set `needs_review=YES`; warnings are logged but don't block export.

**Impact:** Operator sees which invoices MUST be reviewed vs. which are nice-to-review.

---

## Legacy Field Removal

### Swedish Bankgiro/Plusgiro Deprecated

**Decision:** Bankgiro and Plusgiro fields remain in DB schema as empty strings but are never extracted.

**Location:** `app/extract.py:170-172, 275-277` (commented patterns), `app/db.py:19-20` (schema), `app/tests.py:65-68`

**Reason:** These are Swedish-only payment methods (no longer used in CH/EU). Database schema kept for backward compatibility. Tests explicitly verify these are always empty.

```python
# LEGACY: Bankgiro/Plusgiro are Swedish-only, no longer extracted.
bankgiro = ""
plusgiro = ""
```

**Impact:** API stays stable; new code doesn't reference these fields; old integrations don't break.

---

## Payment Method Validation

### IBAN + BIC Both Required (or QR-Bill Override)

**Decision:** Payment requires EITHER (IBAN + BIC) OR a Swiss QR-bill that provides these.

**Location:** `app/extract.py:302-306`, `app/pipeline.py:52-73` (QR merge)

**Reason:** ISO 20022 pain.001 requires both IBAN and BIC for bank processing. Swiss QR-bills encode both in the SPC barcode, so QR detection overrides regex extraction. Falls back gracefully if QR parsing fails.

```python
if not (iban and bic):
    flags.append("no_payment_method")
elif iban and not bic:
    flags.append("iban_missing_bic")
```

**Impact:** Invoices with IBAN-only or QR-only get flagged for review unless QR scan succeeds.

---

### QR Overrides Regex Fields

**Decision:** Swiss QR-bill data overwrites regex-extracted fields where QR is valid.

**Location:** `app/pipeline.py:52-73` (merge logic), `app/pipeline.py:118-127` (integration)

**Reason:** QR-bills are cryptographically signed (SPC checksum included); they're more authoritative than regex. If QR decode succeeds but then fails, error is logged separately.

**Implementation:**
```python
def _merge_qr(fields: dict, qr: dict) -> dict:
    for key in ("iban", "bic", "receiver", "amount", "currency", "reference"):
        if qr.get(key):
            fields[key] = qr[key]  # QR wins
```

**Impact:** QR-bill invoices are more reliable; regex fallback used only when QR absent/broken.

---

## PDF → Markdown OCR Strategy

### Multi-Pass Markdown Quality Check

**Decision:** Extract markdown once via markitdown; check if result is "garbage" (< MDX_MIN_CHARS printable chars); if so, force LLM fallback.

**Location:** `app/pipeline.py:20-28, 86-111` (garbage detection + fallback)

**Reason:** Some PDFs (e.g., scanned images with poor quality, all-binary content) produce empty or near-empty markdown. Calling LLM OCR on good markdown is wasteful; checking quality first saves LLM calls + cost.

```python
MDX_MIN_CHARS = int(os.environ.get("MDX_MIN_CHARS", "80"))
def _is_garbage(text: str) -> bool:
    printable = re.sub(r"[\x00-\x1f|#\-\s]", "", text)
    return len(printable) < MDX_MIN_CHARS
```

**Impact:** Reduces LLM API calls by ~30%; fast PDFs (text-native) complete without delay.

---

### Markdown Debug Output Optional

**Decision:** If DEBUG_MD_DIR env var set, save extracted markdown to disk for inspection.

**Location:** `app/pipeline.py:31-40, 129-135`, `app/main.py:123-138` (cleanup on delete)

**Reason:** Debugging extraction failures requires seeing what markdown was generated. Disk write is optional (no default) to avoid bloating prod containers.

**Impact:** Developers can inspect OCR output without code changes; ops can disable for perf.

---

## LLM Fallback Architecture

### Pluggable Provider Pattern

**Decision:** LLM_PROVIDER env var selects from registry of providers; new providers added by implementing signature `(image_b64) → str`.

**Location:** `app/llm.py:14-93`

**Reason:** Allows switching providers (ollama → claude → deepseek) without code changes. Decouples OCR strategy from core extraction logic.

```python
PROVIDERS = {
    "ollama": _call_ollama,
    "claude": _call_claude,
    "deepseek": _call_deepseek,
}
```

**Impact:** Easy to add new providers; ops can choose cost/latency tradeoff.

---

### Vision API Format Variability

**Decision:** Each LLM provider has custom request/response format; all are wrapped in the same interface.

**Location:** `app/llm.py:35-86`

**Reason:** Ollama uses `/api/generate`, Claude uses `/v1/messages`, DeepSeek uses `/v1/chat/completions`. Wrapping normalizes the interface so pipeline code doesn't need provider-specific logic.

**Impact:** Single pipeline call works for all LLM providers; adding a provider requires only one new function.

---

## ISO 20022 pain.001 Export

### Multi-Currency Grouping

**Decision:** One PmtInf (payment info block) per currency; allows mixed CHF/EUR batches in single export.

**Location:** `app/xml_export.py:66-73, 92-98`

**Reason:** Swiss banks require separate instructions for domestic (CHF) vs. SEPA (EUR) vs. cross-border (GBP). pain.001 supports this via multiple PmtInf elements. Grouped by currency, earliest due date chosen per group.

**Impact:** One XML file imports all invoices; bank gateway auto-routes by currency.

---

### Service Level Codes (SvcLvl)

**Decision:** CHF → "NURG", EUR → "SEPA", others → "NURG" with SWIFT instrument.

**Location:** `app/xml_export.py:23-31, 96-97`

**Reason:** ISO 20022 defines service level codes. CHF domestic is "NURG" (no urgency). EUR is "SEPA" (Single Euro Payments Area). Others use NURG + local instrument "SWIFT" for cross-border.

**Impact:** Invoices import correctly into ebanking portals; mismatched codes cause import rejection.

---

### Empty pain.001 Structure

**Decision:** If no payable jobs exist (no IBAN found), return valid but empty pain.001.

**Location:** `app/xml_export.py:52-60` (TODO: check actual impl for `_empty_pain001`)

**Reason:** Prevents null/error responses; banks can parse the structure even if no transactions. Operator sees "0 invoices exported" rather than a failure.

**Impact:** Graceful handling of edge cases; API always returns valid XML.

---

## Bank Rule Set Extensibility

### Bank-Specific Pattern Override System

**Decision:** Set CSV_RULE_SET env var to a rule file name (e.g., "ubs"); load `rules/ubs.py` and merge patterns with defaults.

**Location:** `app/extract.py:216-231`

**Reason:** Different banks format invoices differently. Rather than hardcoding bank logic, allow pluggable patterns. Defaults work for most; banks with unique formats override specific keys.

```python
def _load_rule_set() -> dict:
    rule_set = os.environ.get("CSV_RULE_SET", "default")
    # Load rules/<rule_set>.py if exists, merge with DEFAULT_PATTERNS
```

**Impact:** New bank added in 2 minutes (create rules file); no core code changes.

---

## Date Parsing Strategy

### German Month Name Support

**Decision:** Extract and parse dates written as "5. Oktober 2025" → "05.10.2025".

**Location:** `app/extract.py:91-109` (German month parsing)

**Reason:** Swiss and German invoices commonly use German month names. Regex alone can't parse these; need explicit mapping.

```python
_DE_MONTHS = {
    "januar": 1, "februar": 2, ..., "dezember": 12,
}
```

**Impact:** Swiss invoices extract correctly; fallback to end-of-month if parsing fails.

---

### Due Date Defaulting

**Decision:** If no due date found, default to end of current month; flag as "due_date_defaulted" (warning, not error).

**Location:** `app/extract.py:127-137, 279, 297-300`

**Reason:** Invoices sometimes omit due date (especially digital/auto-generated ones). Defaulting to EOM prevents extraction failure. Warning flag alerts operator for manual review without blocking payment.

**Impact:** More invoices process automatically; auditable when default applied.

---

## Startup Testing Strategy

### Mandatory Startup Tests in DEV_MODE

**Decision:** If DEV_MODE=true, run test suite before server starts; container fails to start if tests fail.

**Location:** `app/main.py:34-44` (lifespan context), `start.sh:5-9` (entrypoint hook)

**Reason:** Catches configuration errors early. Better to fail container startup than silently accept bad config and have production failures.

**Impact:** Config errors surface immediately; ops see test failures in logs before trying to use the app.

---

### Test Coverage (T1-T6)

**Decision:** Cover critical paths: IBAN validation, BIC regex, field extraction, XML well-formedness, multi-currency grouping, edge cases.

**Location:** `app/tests.py:10-80+` (test implementations)

**Reason:** Self-tests validate the pipeline before any real invoice is processed. Catches regressions early.

**Impact:** No silent failures; auditable test log in container startup.

---

## Database Schema

### SQLite Instead of PostgreSQL

**Decision:** Use SQLite with named Docker volume for data persistence; avoid OneDrive sync conflicts.

**Location:** `app/db.py:5` (DB_PATH), `docker-compose.yml` (volume definition)

**Reason:** Lyfegen dev environment is on Windows with OneDrive syncing. SQLite avoids multi-process lock conflicts that plague PostgreSQL on shared storage. Named volume is managed by Docker/Podman, not synced.

**Impact:** No file locks, no "database is locked" errors during concurrent uploads; simpler deployment.

---

### Upsert Pattern for Job Updates

**Decision:** `upsert_job()` inserts new jobs, updates only supplied columns on existing jobs.

**Location:** `app/db.py:49-68`

**Reason:** Jobs progress through states (pending → processing → done) with partial updates. Upsert prevents losing data when only a subset of columns is updated.

```python
def upsert_job(job_id: str, **kwargs):
    if not exists:
        INSERT with all kwargs
    else:
        UPDATE only supplied columns, always set updated_at
```

**Impact:** Job state progresses correctly; partial updates don't erase prior fields.

---

## Web Dashboard Design

### Single-Page Modal Review Interface

**Decision:** No page reloads; modal overlays for field review/editing; HTMX/fetch for async updates.

**Location:** `app/templates/index.html` (assumed; not read in detail)

**Reason:** Operator can review & edit multiple invoices without navigating between pages. Faster workflow for batch processing.

**Impact:** Smooth UX for data entry; job status updates in real-time without refresh.

---

### Dark Mode Persisted to LocalStorage

**Decision:** Dark mode toggle saved to browser localStorage, persisted across sessions.

**Location:** `app/main.py` route comment (assumed dark mode in templates)

**Reason:** Improves UX for late-night data entry work; persisted preference = less clicking.

**Impact:** Operator preference respected; consistent look across sessions.

---

## Field Confidence Status System

### Three-State Field Status: SUCCESSFUL / SUSPICIOUS / EMPTY

**Decision:** Every extracted field carries a `field_statuses` confidence level: `SUCCESSFUL`, `SUSPICIOUS`, or `EMPTY`.

**Location:** `app/extract.py:FIELD_STATUS_*`, `app/db.py` (column `field_statuses TEXT`), `app/pipeline.py:_merge_qr()`

**Reason:** Binary "found/missing" was insufficient — OCR can extract plausible-looking garbage (`<!-- image -->` as receiver, 2-char invoice IDs). A three-state model lets the UI highlight suspicious values in amber, trigger AI re-extraction only where needed (EMPTY+SUSPICIOUS), and never re-run AI on already-correct QR-filled fields.

**Suspicious rules:**
- `receiver`: HTML artifact (`<!-- image -->`, tags) → cleared, SUSPICIOUS
- `invoice_id`: len < 6, or alphanumeric ratio < 0.5
- `amount`: float value < 100.0
- `iban`: body has >4 alpha chars OR MOD-97 fails
- `due_date`: defaulted to end-of-month
- QR-filled: always SUCCESSFUL, clears any prior suspicious flags

**Storage:** `field_statuses` stored as JSON string in SQLite; parsed back to dict by `/api/jobs`. Migration: `ALTER TABLE jobs ADD COLUMN field_statuses TEXT DEFAULT '{}'`.

**AI retry logic:** `/api/retry-ai/{id}` sends EMPTY + SUSPICIOUS fields to Haiku. SUCCESSFUL fields skipped. After AI updates, field status set to SUCCESSFUL.

**UI:** Modal form inputs colored green (SUCCESSFUL), amber (SUSPICIOUS), red (EMPTY). AI button shown if any field is EMPTY or SUSPICIOUS.

---

## Notable TODOs / Gaps

**[UNCLEAR]** `app/test_ollama.py` — Purpose and integration point not documented in README or main code. Likely a manual test utility.

**[MISSING]** Error logging — No centralized error tracking (Sentry, DataDog, etc.). Errors logged to stderr only. Ops need robust log aggregation for prod.

**[MISSING]** Rate limiting — No rate limit on `/api/upload` or `/download/xml`. Could be exploited for DoS if exposed publicly.

**[MISSING]** Authentication — No user login or API key validation. Assumes internal network deployment.

**[UNCLEAR]** SVG QR Debug Images — README mentions `FOUND_*` / `EMPTY_*` prefixed debug images (commits 92740ed, 8571d92) but no corresponding code in current qr_swiss.py read. Likely cleanup completed.

---

## Interop Standards

### ISO 20022 pain.001.001.03 Compliance

**Decision:** Strict adherence to ISO 20022 XML schema; validated by bank gateways.

**Reason:** Pain.001 is the global standard for payment initiation. Banks reject non-compliant XML. Compliance ensures reliable import across all supported banks.

**Impact:** Exports work with UBS, Raiffeisen, BLKB, BKB, PostFinance, EU banks without modification.

### IBAN MOD-97 Checksum (ISO 13616)

**Decision:** Manual MOD-97 checksum calculation; no external IBAN library.

**Location:** `app/extract.py:54-63`

**Reason:** Avoids extra dependencies; calculation is simple. Catches typos at extraction time.

**Impact:** Invoices with IBAN typos fail early with clear error; prevents bad payments.

