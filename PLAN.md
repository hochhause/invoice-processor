# Implementation Plan — feat/llm-first

> Each step is designed to be completed in an independent session.
> Steps are ordered by dependency — complete them in sequence.
> Start each session by reading the linked reference files listed under **Context** for that step.

---

## Step 0 — Branch Setup

**Recommended model:** `claude-haiku-4-5-20251001` · **Effort: low** — mechanical file ops, no reasoning required.

**Context for this session:**
- [PLAN.md](PLAN.md) — this file (steps overview + appendix)

**Goal:** Create the working branch and remove deleted files.

**Actions:**
1. `git checkout master && git checkout -b feat/llm-first`
2. Delete `app/extract.py`
3. Delete `app/md_clean.py`
4. Delete `app/rules/` (entire directory)
5. Delete `app/test_ollama.py`
6. Commit: `chore: remove regex pipeline, bank rules, legacy test`

**Verify:** `git status` shows clean tree on new branch.

---

## Step 1 — Database Migration (`app/db.py`)

**Recommended model:** `claude-sonnet-4-6` · **Effort: low** — schema is fully specified in this plan, no ambiguity. Low effort Sonnet beats high effort Haiku here: migration logic touches multiple query patterns that benefit from Sonnet's synthesis.

**Context for this session:**
- [PLAN.md](PLAN.md) — Step 1 spec + Appendix (status enum, bank_target routing table, QR field coverage)
- [Notes/DECISIONS.md](Notes/DECISIONS.md) — sections: "DB Schema: pain.001 Fields Only", "Bank Routing via bank_target Column", "Archived Status Instead of Delete"
- [Notes/PROJECT_CONTEXT.md](Notes/PROJECT_CONTEXT.md) — sections: "Database Schema", "New Job Statuses"

**Goal:** Replace the existing schema with the pain.001-only schema. Add migration for existing DBs.

**New schema:**
```sql
CREATE TABLE jobs (
  id           TEXT PRIMARY KEY,
  filename     TEXT NOT NULL,
  status       TEXT NOT NULL DEFAULT 'needs_llm',
  receiver     TEXT DEFAULT '',
  iban         TEXT DEFAULT '',
  bic          TEXT DEFAULT '',
  amount       TEXT DEFAULT '',
  currency     TEXT DEFAULT '',
  due_date     TEXT DEFAULT '',
  reference    TEXT DEFAULT '',
  invoice_id   TEXT DEFAULT '',
  bank_target  TEXT DEFAULT '',   -- BKB | RAIFFEISEN | MANUAL
  created_at   TEXT DEFAULT (datetime('now')),
  updated_at   TEXT DEFAULT (datetime('now'))
);
```

**Status enum:** `needs_llm | qr_done | llm_done | needs_review | archived | error`

**bank_target routing function** (add to db.py):
```python
def derive_bank_target(currency: str) -> str:
    c = (currency or "").upper()
    if c in ("CHF", "SEK", "EUR"): return "BKB"
    if c in ("USD", "CAD", "GBP"): return "RAIFFEISEN"
    return "MANUAL"
```

**Migration script** (run once on existing DBs):
- Add new columns with ALTER TABLE if they don't exist
- Drop: `bankgiro`, `plusgiro`, `review_reasons`, `flags`, `field_statuses`, `error_msg`, `ocr_method`
- Remap old `status` values: `done` → `llm_done`, `pending` → `needs_llm`
- Set `bank_target` for all existing rows via `derive_bank_target(currency)`

**Upsert:** Update `upsert_job()` to only accept new column names.

**New queries needed:**
- `get_jobs(include_archived=False)` — default hides archived
- `get_jobs_by_status(status)` — for LLM batch
- `get_jobs_by_bank(bank_target)` — for export
- `set_bank_target(job_id, bank_target)`
- `archive_jobs(job_ids: list)` — sets status=archived

**Files changed:** `app/db.py`
**Files unchanged:** all others

---

## Step 2 — QR Pipeline on Upload (`app/pipeline.py`)

**Recommended model:** `claude-haiku-4-5-20251001` · **Effort: high** — both functions are fully specified with pseudocode in this plan. High effort Haiku is cheaper than Sonnet and the extra thinking budget handles the merge-priority logic correctly.

**Context for this session:**
- [PLAN.md](PLAN.md) — Step 2 spec + Appendix (QR field coverage table, status flow diagram)
- [Notes/DECISIONS.md](Notes/DECISIONS.md) — sections: "QR-on-Upload, LLM-on-Demand", "QR Invoices Skip LLM (with exceptions)"
- [Notes/Features.md](Notes/Features.md) — section: "1. Drag-Drop PDF Upload + Immediate QR Scan"
- [Notes/PROJECT_CONTEXT.md](Notes/PROJECT_CONTEXT.md) — section: "Pipeline Architecture → Step 1"

**Goal:** QR scan runs synchronously on upload. Returns structured fields immediately. `app/qr_swiss.py` is unchanged — do not modify it.

**Rewrite `app/pipeline.py`** — two functions only:

```python
def run_qr(pdf_path: str) -> dict:
    """
    Run QR scan on upload. Returns fields dict + status.
    Called synchronously — must be fast.
    """
    qr = qr_swiss.extract_from_pdf(pdf_path)
    if qr is None:
        return {"status": "needs_llm"}

    fields = {
        "status": "qr_done",
        "receiver":  qr.get("receiver", ""),
        "iban":      qr.get("iban", ""),
        "bic":       "",            # QR never has BIC
        "amount":    qr.get("amount", ""),
        "currency":  qr.get("currency", ""),
        "reference": qr.get("reference", ""),
        "invoice_id": "",           # QR never has invoice_id
        "due_date":  "",            # QR never has due_date
        "bank_target": db.derive_bank_target(qr.get("currency", "")),
    }

    if fields["iban"] and not _validate_iban(fields["iban"]):
        fields["iban"] = ""
        fields["status"] = "needs_review"

    return fields


def run_llm(pdf_path: str, existing_fields: dict) -> dict:
    """
    Run LLM extraction. Merges into existing_fields (QR fields win).
    Called from /api/run-llm-batch — not on upload.
    """
    llm_fields = llm.extract_fields(pdf_path)

    if llm_fields is None:
        return {**existing_fields, "status": "needs_review"}

    # QR-extracted fields win over LLM
    merged = {**llm_fields, **{k: v for k, v in existing_fields.items() if v}}
    merged["bank_target"] = db.derive_bank_target(merged.get("currency", ""))

    if merged.get("iban") and not _validate_iban(merged["iban"]):
        merged["iban"] = ""

    mandatory = ["receiver", "iban", "amount", "currency"]
    merged["status"] = "llm_done" if all(merged.get(f) for f in mandatory) else "needs_review"

    return merged


def _validate_iban(iban: str) -> bool:
    """MOD-97 checksum. Moved here from deleted extract.py."""
    iban = re.sub(r"\s", "", iban).upper()
    if len(iban) < 5: return False
    rearranged = iban[4:] + iban[:4]
    numeric = "".join(str(ord(c) - 55) if c.isalpha() else c for c in rearranged)
    return int(numeric) % 97 == 1
```

**Files changed:** `app/pipeline.py`
**Files deleted (Step 0):** `app/extract.py`, `app/md_clean.py`, `app/rules/`

---

## Step 3 — LLM Client Rewrite (`app/llm.py`)

**Recommended model:** `claude-haiku-4-5-20251001` · **Effort: high** — the entire file is specified in this plan verbatim. High effort Haiku handles the JSON error-handling edge cases well and costs ~60% less than Sonnet low for what is essentially a transcription task.

**Context for this session:**
- [PLAN.md](PLAN.md) — Step 3 spec
- [Notes/DECISIONS.md](Notes/DECISIONS.md) — sections: "LLM Returns Structured JSON", "Claude Haiku Only"
- [Notes/Features.md](Notes/Features.md) — section: "3. Manual LLM Batch Trigger"

**Goal:** Single provider (Claude Haiku). Returns structured dict, not raw text. No Ollama, no DeepSeek.

**New `app/llm.py`:**

```python
import anthropic, base64, fitz, io, json, os

MODEL = os.environ.get("LLM_MODEL", "claude-haiku-4-5-20251001")

PROMPT = """You are an invoice field extractor.
Return ONLY a valid JSON object with exactly these keys:
  invoice_id, receiver, amount, currency, due_date, iban, bic, reference

Rules:
- amount: decimal string like "1234.56", no currency symbol
- currency: ISO 4217 three-letter code (CHF, EUR, USD, etc.)
- due_date: YYYY-MM-DD format
- iban: no spaces
- Use null for missing fields
- No markdown, no explanation, only the JSON object
"""

def extract_fields(pdf_path: str) -> dict | None:
    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key:
        raise EnvironmentError("ANTHROPIC_API_KEY not set")

    client = anthropic.Anthropic(api_key=api_key)
    images = _pdf_to_images(pdf_path)

    content = []
    for img_b64 in images:
        content.append({
            "type": "image",
            "source": {"type": "base64", "media_type": "image/png", "data": img_b64}
        })
    content.append({"type": "text", "text": PROMPT})

    try:
        response = client.messages.create(
            model=MODEL,
            max_tokens=512,
            messages=[{"role": "user", "content": content}]
        )
        raw = response.content[0].text.strip()
        parsed = json.loads(raw)
        return {k: (v or "") for k, v in parsed.items()}
    except (json.JSONDecodeError, KeyError, anthropic.APIError) as e:
        print(f"[llm] extraction failed: {e}", flush=True)
        return None


def _pdf_to_images(pdf_path: str, dpi: int = 150) -> list[str]:
    doc = fitz.open(pdf_path)
    images = []
    for page in doc:
        pix = page.get_pixmap(dpi=dpi)
        images.append(base64.b64encode(pix.tobytes("png")).decode())
    doc.close()
    return images
```

**Env vars required:**
```env
ANTHROPIC_API_KEY=sk-ant-...
LLM_MODEL=claude-haiku-4-5-20251001
```

**Files changed:** `app/llm.py`
**Files unchanged:** all others

---

## Step 4 — API Routes Rewrite (`app/main.py`)

**Recommended model:** `claude-opus-4-8` · **Effort: medium** — largest rewrite in the plan. Must read and reconcile 4 completed files (db.py, pipeline.py, llm.py, xml_export.py) and produce a coherent main.py. Medium effort gives Opus enough thinking budget to handle cross-file dependencies without burning max tokens on a well-scoped task. Opus over Sonnet justified here: retry cost from a botched main.py rewrite exceeds the price delta.

**Context for this session:**
- [PLAN.md](PLAN.md) — Step 4 spec + Appendix (API routes table, status flow diagram)
- [Notes/PROJECT_CONTEXT.md](Notes/PROJECT_CONTEXT.md) — sections: "API Routes", "Pipeline Architecture"
- [Notes/Features.md](Notes/Features.md) — sections: "1. Upload", "3. LLM Batch", "5. Export Screen", "6. pain.001 Export", "9. Job Lifecycle"
- [Notes/DECISIONS.md](Notes/DECISIONS.md) — sections: "QR-on-Upload", "Unsorted Invoices Excluded from Download", "Archived Status Instead of Delete"
- `app/db.py` — read current file (completed in Step 1) for query signatures
- `app/pipeline.py` — read current file (completed in Step 2) for function signatures

**Goal:** Remove all regex/markitdown routes. Add LLM batch, bank assignment, export confirm routes.

**Keep (modify):**
- `POST /api/upload` — call `pipeline.run_qr()` synchronously (no BackgroundTask for QR)
- `GET /api/jobs` — add `?include_archived=true` param; remove legacy fields from JSON response
- `POST /api/review/{id}` — update accepted fields to new schema
- `GET /api/pdf/{id}` — unchanged
- `DELETE /api/jobs/{id}` — unchanged
- `DELETE /api/clear-all` — only clear non-archived jobs
- `GET /download/csv` — update column list to new schema

**Add:**
- `POST /api/run-llm-batch` — BackgroundTask; processes all `needs_llm` + QR edge cases
- `POST /api/assign-bank/{id}` — body `{"bank_target": "BKB|RAIFFEISEN|MANUAL"}`
- `GET /export` — render `export.html` template
- `POST /download/confirm` — generate both pain.001 files, archive sorted jobs, return zip

**Remove:**
- `POST /process/{id}` (legacy single reprocess)
- `POST /process-all`
- `POST /api/queue-llm-all`
- All `force_llm` parameter handling
- `POST /api/dev-fill-all`

**LLM batch route:**
```python
@app.post("/api/run-llm-batch")
async def run_llm_batch(background_tasks: BackgroundTasks):
    jobs = db.get_jobs_needing_llm()
    background_tasks.add_task(_run_llm_batch, jobs)
    return {"queued": len(jobs)}

async def _run_llm_batch(jobs):
    for job in jobs:
        fields = pipeline.run_llm(job["pdf_path"], dict(job))
        db.upsert_job(job["id"], **fields)
```

**Download confirm route:**
```python
@app.post("/download/confirm")
async def download_confirm():
    bkb_jobs = db.get_jobs_by_bank("BKB")
    raiff_jobs = db.get_jobs_by_bank("RAIFFEISEN")
    bkb_xml = xml_export.build_pain001(bkb_jobs, debtor, bank="BKB")
    raiff_xml = xml_export.build_pain001(raiff_jobs, debtor, bank="RAIFFEISEN")
    db.archive_jobs([j["id"] for j in bkb_jobs + raiff_jobs])
    return zip_response([("pain001_BKB.xml", bkb_xml), ("pain001_Raiffeisen.xml", raiff_xml)])
```

**Files changed:** `app/main.py`

---

## Step 5 — XML Export Update (`app/xml_export.py`)

**Recommended model:** `claude-haiku-4-5-20251001` · **Effort: high** — surgical edits to an existing file. Service level codes and filter logic are fully specified. High effort Haiku handles the ISO 20022 domain vocabulary fine given the spec; Sonnet adds cost without benefit here.

**Context for this session:**
- [PLAN.md](PLAN.md) — Step 5 spec + Appendix (bank_target routing table)
- [Notes/DECISIONS.md](Notes/DECISIONS.md) — sections: "ISO 20022 pain.001 Multi-Currency Grouping", "Service Level Codes (SvcLvl)"
- [Notes/Features.md](Notes/Features.md) — section: "6. pain.001 XML Export (per bank)"
- `app/xml_export.py` — read current file before editing

**Goal:** Accept `bank` parameter filter. Correct service levels per bank.

**Changes:**
- `build_pain001(jobs, debtor, bank: str = None)` — filter by `bank_target` if `bank` supplied
- Service levels:
  - BKB file: CHF → NURG, SEK → NURG+SWIFT, EUR → SEPA
  - Raiffeisen file: USD/CAD/GBP → NURG+SWIFT
- If no jobs after filter → raise HTTP 400 (remove `_empty_pain001` fallback)
- No XML schema changes

**Files changed:** `app/xml_export.py`

---

## Step 6 — Dashboard UI Rework (split files)

**Recommended model:** `claude-sonnet-4-6` · **Effort: medium** — reads existing monolith index.html, extracts implicit JS/polling patterns, then produces four new files. Splitting adds structural decisions that benefit from Sonnet's synthesis; Haiku high would likely produce tangled import paths or miss shared state between JS modules.

**Context for this session:**
- [PLAN.md](PLAN.md) — Step 6 spec (file split map, design tokens, table columns, modal layout)
- [Notes/Features.md](Notes/Features.md) — sections: "2. Real-Time Job Status Table", "4. Full-Page Edit Modal", "3. Manual LLM Batch Trigger"
- [Notes/export_screen_poc.html](Notes/export_screen_poc.html) — **primary style reference** — copy CSS variables, card styles, modal structure, topbar pattern verbatim
- `app/templates/index.html` — read current monolith to extract polling logic, modal state, HTMX patterns before splitting
- `app/static/css/` and `app/static/js/` — read existing files to avoid clobbering

**Goal:** Break the monolithic index.html into focused files. Apply Lyfegen brand tokens + POC dark UI. Steps 1–5 are already complete — do not touch any Python files.

---

### File Split Map

```
app/
├── templates/
│   ├── index.html                  ← lean shell: topbar + table + {% include %} only
│   └── partials/
│       └── modal_edit.html         ← full edit modal HTML (reused by export.html in Step 7)
└── static/
    ├── css/
    │   └── main.css                ← all CSS: variables, layout, table, modal, badges
    └── js/
        ├── dashboard.js            ← table polling, row actions, LLM batch button
        └── modal.js                ← modal open/close/save/save-next, bank pills, field colors
```

**`index.html` after split (skeleton only):**
```html
<!DOCTYPE html>
<html lang="en">
<head>
  <link rel="stylesheet" href="/static/css/main.css">
</head>
<body>
  <!-- topbar -->
  <div class="topbar">...</div>
  <!-- jobs table -->
  <table id="jobs-table">...</table>
  <!-- edit modal (shared partial) -->
  {% include 'partials/modal_edit.html' %}
  <script src="/static/js/modal.js"></script>
  <script src="/static/js/dashboard.js"></script>
</body>
</html>
```

**`partials/modal_edit.html`:** contains only the modal `<div>` — no `<html>`, no `<script>` tags. JS lives in `modal.js`. Step 7 `export.html` will `{% include 'partials/modal_edit.html' %}` instead of duplicating.

---

### `main.css` — Lyfegen brand tokens

```css
:root {
  --bg:        #0a1a18;    /* near Dark Teal #003438 */
  --surface:   #0f2523;
  --surface2:  #163330;
  --border:    #1e4440;
  --accent:    #22BAA0;    /* Lyfegen Primary Teal */
  --accent-dim:#0f3d37;
  --text:      #F3F3F2;    /* Lyfegen Off-White */
  --text-dim:  #6b9e99;
  --green:     #22BAA0;
  --amber:     #f59e0b;
  --red:       #ef4444;
  font-family: 'Satoshi', 'Aptos', system-ui, sans-serif;
}
```

Includes styles for: topbar, table, status badges, bank-target chips, modal overlay, form grid, bank pills, scrollbars. Everything from the POC `<style>` block belongs here.

---

### `dashboard.js` responsibilities

- Poll `GET /api/jobs` every 2s (only while any job has `status ∈ {LLM-Pending, needs_review}`)
- Render table rows (receiver, amount, currency, due_date, iban, status badge, bank_target chip, edit button)
- Topbar: show/hide "Run AI Extraction" based on `LLM-Pending` count
- "Run AI Extraction" click → `POST /api/run-llm-batch` → show spinner in topbar
- "Export →" click → `window.location = '/export'`
- Edit icon click → calls `openModal(jobId)` from `modal.js`

**Table columns:** Filename, Receiver, Amount, Currency, Due Date, IBAN, Status, Bank Target, Actions

**Status badge colors:**
- `QR-processed` → teal pill
- `LLM-Pending` → amber pill
- `LLM-Done` → blue pill
- `needs_review` → red pill
- `archived` → gray pill (hidden by default; shown with `?include_archived=true`)

---

### `modal.js` responsibilities

- `openModal(jobId, jobIndex, totalJobs)` — fetch job from cached list, populate form, show overlay
- `closeModal()` — hide overlay, discard unsaved changes
- `saveModal()` → `POST /api/review/{id}` with form data
- `saveAndNext()` → save then advance to next job index, call `openModal`
- Bank pill click → `POST /api/assign-bank/{id}`, update active pill highlight
- Field coloring: green (filled), amber (empty but non-blocking), red (empty + mandatory)
- Exports: `window.openModal`, `window.closeModal` — callable from both `dashboard.js` and `export.html`

---

### `partials/modal_edit.html` structure

```html
<div class="modal-overlay" id="modal">
  <div class="modal">
    <div class="modal-header">
      <h2 id="modal-title"></h2>
      <span id="modal-nav"></span>
      <button onclick="closeModal()">✕</button>
    </div>
    <div class="modal-body">
      <div class="modal-pdf">
        <iframe id="pdf-frame" src=""></iframe>
      </div>
      <div class="modal-form">
        <!-- Payment Details section -->
        <!-- Bank Details section -->
        <!-- Bank Assignment pills -->
      </div>
    </div>
    <div class="modal-footer">
      <button class="btn-ghost" onclick="closeModal()">Close</button>
      <div class="spacer"></div>
      <button class="btn-ghost" onclick="saveModal()">Save</button>
      <button class="btn-primary" onclick="saveAndNext()">Save &amp; Next →</button>
    </div>
  </div>
</div>
```

Form fields: `receiver`, `invoice_id`, `amount`, `currency`, `due_date`, `iban`, `bic`, `reference`
Bank pills: BKB / Raiffeisen / Manual

---

**Files created/changed:**

| File | Action |
|------|--------|
| `app/templates/index.html` | Rewrite as lean shell |
| `app/templates/partials/modal_edit.html` | **New** — extracted modal HTML |
| `app/static/css/main.css` | **New** — all CSS from monolith + Lyfegen tokens |
| `app/static/js/dashboard.js` | **New** — table polling + row actions |
| `app/static/js/modal.js` | **New** — modal lifecycle |

**Files NOT touched (Steps 1–5 complete):** `app/main.py`, `app/db.py`, `app/pipeline.py`, `app/llm.py`, `app/xml_export.py`, `app/qr_swiss.py`

---

## Step 7 — Export Screen (`app/templates/export.html`)

**Recommended model:** `claude-sonnet-4-6` · **Effort: low** — POC already exists; task is wiring API calls and using the shared partial + JS from Step 6. Well-defined. Low effort Sonnet is cheaper than Sonnet medium and the spec leaves no ambiguity.

**Context for this session:**
- [PLAN.md](PLAN.md) — Step 7 spec
- [Notes/export_screen_poc.html](Notes/export_screen_poc.html) — **base file** — copy drag-board HTML/CSS/JS verbatim then wire up real API calls
- [Notes/Features.md](Notes/Features.md) — section: "5. Export Screen — Bank Assignment & Download"
- [Notes/DECISIONS.md](Notes/DECISIONS.md) — sections: "Export Screen as Separate Route", "Unsorted Invoices Excluded from Download", "Archived Status Instead of Delete", "Export Hard-Blocked on Incomplete Invoices"
- [Notes/PROJECT_CONTEXT.md](Notes/PROJECT_CONTEXT.md) — section: "API Routes" (assign-bank, export-readiness, download/confirm endpoints)
- `app/templates/partials/modal_edit.html` — read (completed in Step 6) — include via Jinja2, do NOT duplicate
- `app/static/css/main.css` — already loaded; no new CSS file needed
- `app/static/js/modal.js` — already loaded; `openModal` available globally

**Goal:** Productionize the POC. Wire to real API. Use shared partial + JS from Step 6 — no duplication.

**Template structure:**
```html
<!DOCTYPE html>
<html lang="en">
<head>
  <link rel="stylesheet" href="/static/css/main.css">
  <!-- export-specific styles inline or in main.css -->
</head>
<body>
  <div class="topbar">...</div>
  <div class="main">
    <!-- three bank columns from POC -->
  </div>
  <div class="bottombar">...</div>
  {% include 'partials/modal_edit.html' %}   {# shared — no duplication #}
  <script src="/static/js/modal.js"></script>
  <script src="/static/js/export.js"></script>
</body>
</html>
```

**New file: `app/static/js/export.js`** — drag-board logic only (no modal code):
```javascript
// Init — load jobs, render columns
const jobs = await fetch('/api/jobs').then(r => r.json());

// Drag drop — reassign bank
await fetch(`/api/assign-bank/${id}`, {
  method: 'POST',
  headers: {'Content-Type': 'application/json'},
  body: JSON.stringify({ bank_target: newZone.toUpperCase() })
});

// Pre-flight readiness check
const readiness = await fetch('/api/export-readiness').then(r => r.json());
if (!readiness.ready) { showBlockersPopup(readiness.blockers); return; }

// Accept & Download
const res = await fetch('/download/confirm', { method: 'POST' });
if (res.status === 409) {
  const { blockers } = await res.json();
  showBlockersPopup(blockers);
  return;
}
const blob = await res.blob();
const a = document.createElement('a');
a.href = URL.createObjectURL(blob);
a.download = 'pain001_export.zip';
a.click();
```

**Blockers popup:** inline `<div>` shown when export readiness fails — lists each blocking invoice (filename, status, missing fields). Operator must fix all before download proceeds.

**Column header totals:** Sum amounts per currency per bank. Display: `"CHF 48,320 · EUR 3,100"`.

**Post-download:** Show toast `"✓ N invoices archived"` then `window.location = '/'` after 2s.

**Card click → edit modal:** calls `window.openModal(jobId)` from `modal.js` — no extra code needed.

**Files created:**

| File | Action |
|------|--------|
| `app/templates/export.html` | **New** — lean shell with Jinja2 include |
| `app/static/js/export.js` | **New** — drag-board + download logic |

**Files reused (no changes):** `partials/modal_edit.html`, `main.css`, `modal.js`
**Files NOT touched (Steps 1–5 complete):** all Python files

---

## Step 8 — Tests Update (`app/tests.py`)

**Recommended model:** `claude-haiku-4-5-20251001` · **Effort: high** — explicit remove/keep/add lists in this plan. High effort Haiku ensures T7–T10 test bodies are correct without missing edge cases. Cheapest option that produces reliable test code from a spec.

**Context for this session:**
- [PLAN.md](PLAN.md) — Step 8 spec (remove list, keep list, add list)
- [Notes/DECISIONS.md](Notes/DECISIONS.md) — section: "Mandatory Startup Tests in DEV_MODE"
- `app/tests.py` — read current file before editing
- `app/pipeline.py` — read (completed in Step 2) for `_validate_iban` + `run_qr` signatures
- `app/db.py` — read (completed in Step 1) for `derive_bank_target` signature

**Goal:** Remove regex tests. Add tests for new pipeline paths.

**Remove:**
- T3: `extract_fields()` structure test
- Any reference to `bankgiro`, `plusgiro`, `field_statuses`, `ocr_method`

**Keep:**
- T1: IBAN MOD-97 checksum (now in `pipeline._validate_iban`)
- T2: BIC regex
- T4: pain.001 XML well-formedness
- T5: Multi-currency PmtInf grouping
- T6: Skip jobs with missing IBAN

**Add:**
- T7: `db.derive_bank_target()` — CHF→BKB, USD→RAIFFEISEN, JPY→MANUAL
- T8: `pipeline.run_qr()` with mock QR data → correct fields + status=qr_done
- T9: `pipeline._validate_iban()` — valid CH/DE, invalid, spaces, lowercase
- T10: LLM JSON parsing — valid JSON, null→"", malformed JSON → returns None
- T11: `pipeline.run_vendor_check()` — exact match autofills IBAN+BIC, mismatch sets `document_mismatch`, no vendor entry leaves fields unchanged *(add after Step 11 is complete)*

**Files changed:** `app/tests.py`

---

## Step 9 — Dependencies & Dockerfile

**Recommended model:** `claude-haiku-4-5-20251001` · **Effort: low** — diff is fully specified. Pure config file edits. Lowest cost step in the plan.

**Context for this session:**
- [PLAN.md](PLAN.md) — Step 9 spec (diff tables below)
- [Notes/PROJECT_CONTEXT.md](Notes/PROJECT_CONTEXT.md) — section: "Tech Stack" (removed/kept libraries)
- `requirements.txt` — read current file
- `Dockerfile` — read current file
- `.env.example` — read current file

**`requirements.txt` changes:**
```diff
- markitdown
- pdfplumber
+ anthropic
  pymupdf
  pyzbar
  zxingcpp
  fastapi
  uvicorn
  jinja2
  pillow
  numpy
```

**`Dockerfile` changes:**
- Remove any `apt-get install tesseract` or OCR system packages if present
- Ensure `pip install anthropic` is in the install step

**`.env.example` changes:**
```diff
- LLM_PROVIDER=ollama|claude|deepseek
- LLM_URL=http://host.docker.internal:11434
- LLM_MODEL=llama3.2
- DEEPSEEK_API_KEY=...
- MDX_MIN_CHARS=80
- CSV_RULE_SET=default
+ ANTHROPIC_API_KEY=sk-ant-...
+ LLM_MODEL=claude-haiku-4-5-20251001
  DEV_MODE=true
  DEBTOR_NAME=Lyfegen HealthTech AG
  DEBTOR_IBAN=...
  DEBTOR_BIC=...
  UPLOAD_DIR=/app/data/uploads
  DB_PATH=/app/data/invoices.db
+ DEBUG_QR_DIR=/app/data/debug_qr
```

**Files changed:** `requirements.txt`, `Dockerfile`, `.env.example`

---

## Step 11 — Vendor IBAN Lookup (`app/vendors.py` + migrations)

**Recommended model:** `claude-sonnet-4-6` · **Effort: medium** — touches DB migration, a new module, pipeline logic, two existing completed files (pipeline.py, main.py via additive changes only), and UI badges. Cross-cutting enough to need Sonnet's synthesis; effort medium because the spec is tight and files to read are well-defined.

**Context for this session:**
- [PLAN.md](PLAN.md) — Step 11 spec + Appendix (status flow, DB schema)
- [Notes/DECISIONS.md](Notes/DECISIONS.md) — sections: "DB Schema: pain.001 Fields Only", "QR Invoices Skip LLM"
- [Notes/Features.md](Notes/Features.md) — section: "3. Manual LLM Batch Trigger" (vendor check slots in after LLM merge)
- `app/db.py` — read (completed in Step 1) for `upsert_job`, `get_jobs` signatures
- `app/pipeline.py` — read (completed in Step 2) for `run_qr` / `run_llm` signatures — add `run_vendor_check()` call, do not modify existing functions
- `app/main.py` — read (completed in Step 4) — add vendor CRUD routes + `GET /api/vendors` call in pipeline flow, do not remove or modify existing routes
- `app/static/js/modal.js` — read (completed in Step 6) — understand field coloring contract before adding `iban_source` badge
- `app/static/js/dashboard.js` — read (completed in Step 6) — understand table row render before adding `iban_source` chip

**Goal:** Add a small `vendors` table mapping receiver names → trusted IBAN + BIC. After every QR/LLM extraction, run a vendor lookup: verify the extracted IBAN against the known IBAN (flag mismatch), or autofill if no IBAN was found. Tag every job's IBAN with its source. Steps 1–6 are complete — additive changes only to those files.

---

### New DB objects (migration, not rewrite)

**New table:**
```sql
CREATE TABLE IF NOT EXISTS vendors (
  id           TEXT PRIMARY KEY DEFAULT (lower(hex(randomblob(8)))),
  receiver_name TEXT NOT NULL,          -- matched case-insensitively against jobs.receiver
  iban         TEXT NOT NULL,
  bic          TEXT DEFAULT '',
  created_at   TEXT DEFAULT (datetime('now')),
  updated_at   TEXT DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_vendors_receiver ON vendors(lower(receiver_name));
```

**New column on `jobs` table** (ALTER TABLE migration, does not touch Step 1 schema):
```sql
ALTER TABLE jobs ADD COLUMN iban_source TEXT DEFAULT '';
-- values: 'document' | 'database' | 'llm' | 'manual' | ''
```

Add migration to `db.py` `init_db()` using `ALTER TABLE ... ADD COLUMN IF NOT EXISTS` pattern (SQLite: wrap in try/except `OperationalError`).

---

### New module: `app/vendors.py`

```python
def lookup(receiver: str) -> dict | None:
    """
    Case-insensitive prefix/exact match on vendor receiver_name.
    Returns {"iban": ..., "bic": ...} or None.
    """

def upsert_vendor(receiver_name: str, iban: str, bic: str = "") -> None:

def list_vendors() -> list[dict]:

def delete_vendor(vendor_id: str) -> None:
```

**Matching strategy:** exact match first, then case-insensitive. No fuzzy — false positives on IBAN are a financial risk. Operator must enter the receiver name exactly as it appears in invoices.

---

### New function: `pipeline.run_vendor_check(fields: dict) -> dict`

Called after `run_qr()` or `run_llm()` — never replaces them, always runs after.

```python
def run_vendor_check(fields: dict) -> dict:
    """
    Post-extraction vendor IBAN verification / autofill.
    Mutates iban_source; never changes status.
    """
    vendor = vendors.lookup(fields.get("receiver", ""))
    if not vendor:
        return fields                               # no entry → no change

    extracted_iban = fields.get("iban", "")

    if extracted_iban:
        if extracted_iban == vendor["iban"]:
            fields["iban_source"] = "document"     # confirmed match
        else:
            # Mismatch: flag but do NOT silently overwrite
            fields["iban_source"] = "document_mismatch"
            fields["iban_mismatch_db"] = vendor["iban"]   # stored for UI display
    else:
        # No IBAN from doc/LLM → autofill from DB
        fields["iban"] = vendor["iban"]
        fields["bic"]  = vendor["bic"] or fields.get("bic", "")
        fields["iban_source"] = "database"

    return fields
```

**Integration point in pipeline.py** (additive — append calls, do not rewrite existing functions):
```python
# In run_qr(), before return:
fields = run_vendor_check(fields)
return fields

# In run_llm(), before return:
merged = run_vendor_check(merged)
return merged
```

---

### `iban_source` values

| Value | Meaning | UI display |
|-------|---------|-----------|
| `document` | Extracted from PDF, confirmed by vendor DB | green chip "doc ✓" |
| `document_mismatch` | Extracted from PDF, conflicts with vendor DB | red chip "doc ⚠" — shows DB IBAN in tooltip |
| `database` | Autofilled from vendor DB (no IBAN in doc) | blue chip "DB" |
| `llm` | Extracted by LLM, no vendor entry to verify | gray chip "llm" |
| `manual` | Set by operator in edit modal | gray chip "manual" |
| `''` | QR-provided, no vendor entry | no chip |

**`document_mismatch` handling:** job is NOT automatically sent to `needs_review` — it stays `LLM-Done` or `QR-processed`. The mismatched IBAN is shown in the edit modal with a red warning ("DB has: CH56...") so the operator can judge. The operator's save action sets `iban_source = 'manual'`.

---

### New API routes (add to `main.py` — do not remove existing routes)

| Method | Path | Action |
|--------|------|--------|
| GET | `/api/vendors` | List all vendors |
| POST | `/api/vendors` | Create vendor `{receiver_name, iban, bic}` |
| PUT | `/api/vendors/{id}` | Update vendor |
| DELETE | `/api/vendors/{id}` | Delete vendor |

No dedicated UI page needed — vendor management is accessible from the edit modal ("Add to vendors" button when `iban_source` is empty or `llm`) and from a simple inline table in the dashboard topbar or settings drawer.

---

### UI changes (additive to completed Step 6 files)

**`app/static/js/dashboard.js`** — add `iban_source` chip to table rows:
- render chip next to IBAN cell based on `job.iban_source`
- `document_mismatch` → red chip, hover tooltip shows vendor IBAN

**`app/static/js/modal.js`** — in edit modal:
- Show `iban_source` badge next to IBAN field label
- If `iban_source === 'document_mismatch'`: show red warning block under IBAN input: `"⚠ Vendor DB has: {iban_mismatch_db} — verify before saving"`
- "Add to vendors" button (shown when `iban_source ∈ {'llm', ''}` and IBAN is non-empty): calls `POST /api/vendors` with current receiver + IBAN, refreshes source badge to `database`

**`app/templates/partials/modal_edit.html`** — add `iban_source` badge span and mismatch warning `<div>` (hidden by default, shown by `modal.js`).

---

### `iban_mismatch_db` storage

Stored in DB as a separate column (not pain.001 relevant, diagnostic only):
```sql
ALTER TABLE jobs ADD COLUMN iban_mismatch_db TEXT DEFAULT '';
```
Cleared on operator save (upsert with `iban_mismatch_db = ''`).

---

**Files changed/created:**

| File | Action | Note |
|------|--------|------|
| `app/vendors.py` | **New** | Lookup, CRUD, index |
| `app/db.py` | Additive migration | `ALTER TABLE` for `iban_source`, `iban_mismatch_db`; `vendors` table |
| `app/pipeline.py` | Additive | `run_vendor_check()` appended; `run_qr`/`run_llm` call it at end |
| `app/main.py` | Additive | 4 vendor CRUD routes added |
| `app/static/js/dashboard.js` | Additive | `iban_source` chip in table rows |
| `app/static/js/modal.js` | Additive | Mismatch warning + "Add to vendors" button |
| `app/templates/partials/modal_edit.html` | Additive | Badge span + mismatch `<div>` |

**Files NOT touched:** `app/llm.py`, `app/xml_export.py`, `app/qr_swiss.py`, `app/tests.py` (T11 added in Step 8 — see below), all templates except the partial above.

---

## Step 10 — Integration Testing

**Recommended model:** `claude-sonnet-4-6` · **Effort: medium** — exploratory: must start the app, observe real behavior, adapt when something breaks. Medium effort gives enough thinking budget to diagnose failures without burning Opus-level tokens on what is mostly observation + small fixes. Use `/verify` or `/run` skill.

**Context for this session:**
- [PLAN.md](PLAN.md) — Step 10 test cases
- [Notes/Features.md](Notes/Features.md) — "Cross-Feature Flows" section (happy paths)
- [Notes/DECISIONS.md](Notes/DECISIONS.md) — "Dev/Prod Mode Split" section
- All completed app files (read as needed during testing)

**Goal:** End-to-end smoke test with real PDFs before merging to master.

**Test cases:**
1. Upload Swiss QR invoice → status=`QR-processed`, bank=`BKB`, fields populated
2. Upload non-QR invoice → status=`LLM-Pending`
3. Trigger "Run AI Extraction" → status=`LLM-Done` or `needs_review`
4. Open edit modal → Save & Next through 3 invoices
5. Navigate to `/export` → drag one invoice between columns → bank reassigned
6. Accept & Download → two XML files downloaded, jobs archived
7. Return to dashboard → archived jobs not visible in table
8. CSV export → all expected columns present, archived excluded
9. DEV_MODE tests T1–T11 pass before container starts
10. Add a vendor entry (receiver + IBAN) via `POST /api/vendors`
11. Upload invoice with matching receiver, no IBAN in PDF → `iban_source=database`, IBAN autofilled
12. Upload invoice with matching receiver, different IBAN in PDF → `iban_source=document_mismatch`, mismatch warning visible in modal
13. Upload invoice with matching receiver, same IBAN in PDF → `iban_source=document`, green chip shown
14. "Add to vendors" button in modal → creates vendor entry, badge updates to `database`

**Merge to master** once all cases pass.

---

## Appendix — File Change Summary

| File | Action | Step |
|------|--------|------|
| `app/extract.py` | **Delete** | 0 |
| `app/md_clean.py` | **Delete** | 0 |
| `app/rules/` | **Delete** | 0 |
| `app/test_ollama.py` | **Delete** | 0 |
| `app/db.py` | Rewrite | 1 |
| `app/pipeline.py` | Rewrite | 2 |
| `app/llm.py` | Rewrite | 3 |
| `app/main.py` | Rewrite | 4 |
| `app/xml_export.py` | Update | 5 |
| `app/templates/index.html` | Rewrite as lean shell | 6 |
| `app/templates/partials/modal_edit.html` | New — shared modal partial | 6 |
| `app/static/css/main.css` | New — all CSS + Lyfegen tokens | 6 |
| `app/static/js/dashboard.js` | New — table polling + actions | 6 |
| `app/static/js/modal.js` | New — modal lifecycle | 6 |
| `app/templates/export.html` | New — lean shell with Jinja2 include | 7 |
| `app/static/js/export.js` | New — drag-board + download logic | 7 |
| `app/tests.py` | Update (T11 after Step 11) | 8 |
| `app/vendors.py` | **New** — vendor lookup + CRUD | 11 |
| `app/db.py` | Additive migration (`iban_source`, `iban_mismatch_db`, `vendors` table) | 11 |
| `app/pipeline.py` | Additive (`run_vendor_check` + calls) | 11 |
| `app/main.py` | Additive (4 vendor routes) | 11 |
| `app/static/js/dashboard.js` | Additive (`iban_source` chip) | 11 |
| `app/static/js/modal.js` | Additive (mismatch warning + "Add to vendors") | 11 |
| `app/templates/partials/modal_edit.html` | Additive (badge span + mismatch div) | 11 |
| `requirements.txt` | Update | 9 |
| `Dockerfile` | Update | 9 |
| `.env.example` | Update | 9 |
| `app/qr_swiss.py` | **No change** | — |
| `app/static/` | Minor CSS only | 6 |

---

## Appendix — Status Flow

```
upload
  ├── QR found → qr_done
  └── no QR   → needs_llm
                    ↓ (POST /api/run-llm-batch)
              llm_done | needs_review
                    ↓ (POST /download/confirm)
                 archived
```

---

## Appendix — bank_target Routing

| Currency | Target |
|----------|--------|
| CHF, SEK, EUR | BKB |
| USD, CAD, GBP | RAIFFEISEN |
| anything else | MANUAL |

---

## Appendix — QR Field Coverage

| Field | QR provides? | LLM fills? | Notes |
|-------|-------------|-----------|-------|
| iban | ✓ | ✓ | QR wins on merge |
| receiver | ✓ | ✓ | QR wins on merge |
| amount | ✓ (can be blank) | ✓ | |
| currency | ✓ | ✓ | |
| reference | ✓ | ✓ | |
| bic | ✗ | ✓ | Not needed for CHF NURG |
| due_date | ✗ | ✓ | LLM fills for all |
| invoice_id | ✗ | ✓ | Audit only, not pain.001 blocking |

**CHF QR invoices:** BIC not required for domestic NURG payments — LLM skips these unless currency ≠ CHF.

---

## Appendix — API Routes (Final State)

| Method | Path | Action |
|--------|------|--------|
| GET | `/` | Dashboard |
| GET | `/export` | Export screen |
| POST | `/api/upload` | Upload PDF + sync QR scan |
| GET | `/api/jobs` | List jobs (excludes archived by default) |
| POST | `/api/review/{id}` | Save edited fields |
| POST | `/api/run-llm-batch` | Trigger LLM on all needs_llm jobs |
| POST | `/api/assign-bank/{id}` | Override bank_target |
| GET | `/api/pdf/{id}` | Serve original PDF |
| POST | `/download/confirm` | Generate + zip both pain.001 files, archive |
| GET | `/download/csv` | Export CSV |
| DELETE | `/api/jobs/{id}` | Delete single job |
| DELETE | `/api/clear-all` | Wipe all non-archived jobs |
