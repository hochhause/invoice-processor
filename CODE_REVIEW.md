# Python Code Review — invoice-processor

**Scope:** all 11 `.py` files in the repo (`app/*.py` + `rules/default.py`). The requested `/app/templates` path contains only Jinja2 HTML, no Python, so the audit was redirected to the application code at the user's direction (main.py-first).

**Method:** per-file review against the checklist (naming / conventions / dead code / cross-file / bugs), then **adversarial verification** of each concrete bug claim by an independent skeptic agent prompted to *refute* it. Verdicts are tagged:

- ✅ **VERIFIED** — independent agent could not refute it; real defect.
- ❌ **REFUTED** — independent agent showed it is unreachable/benign/stylistic.
- ⚠️ **UNVERIFIED** — the verifier hit a session limit; verdict is my own static analysis.

---

## Summary

The codebase is small, readable, and reasonably well-structured for a single-operator internal tool: a clean FastAPI layer ([main.py](app/main.py)), a thin SQLite DAL ([db.py](app/db.py)), an extraction pipeline ([pipeline.py](app/pipeline.py)) over a QR decoder ([qr_swiss.py](app/qr_swiss.py)) and an LLM client ([llm.py](app/llm.py)), and an ISO-20022 exporter ([xml_export.py](app/xml_export.py)). Docstrings exist on most public surfaces, type hints are present (if inconsistent), and there is a startup self-test suite ([tests.py](app/tests.py)).

**The most critical issues are not style — they are correctness and stale state:**

1. **🔴 CRITICAL — every successful QR upload fails to persist on a fresh DB.** [pipeline.py:23](app/pipeline.py#L23) emits a `due_date` key that is **not a column** in the `jobs` schema ([db.py:8-25](app/db.py#L8-L25)), and [main.py:171-172](app/main.py#L171-L172) passes it **unfiltered** into `upsert_job`, whose dynamic INSERT interpolates kwargs keys as columns. Result (verified empirically): `sqlite3.OperationalError: table jobs has no column named due_date`, **zero rows written, HTTP 500**. This is the app's primary happy path.

2. **🟠 Analytics undercount (the recent "fix" doesn't work).** [main.py:262](app/main.py#L262) counts archived QR invoices with `match_type IS NULL`, but `match_type` defaults to `''` (not NULL) in the schema and is never set for QR jobs — so the clause matches **nothing**. Commit `0221fbb`'s stated goal is unmet.

3. **🟠 Total-extraction-failure mislabeled as `image_only`.** [pipeline.py:61-62](app/pipeline.py#L61-L62) hard-codes `match_type="image_only"` when *both* LLM stages fail, persisting a failure as if the image stage succeeded — pollutes the dashboard Image-Only metric and double-counts the row.

4. **🟠 Silent vendor-BIC loss on cross-border invoices.** [pipeline.py:83-84](app/pipeline.py#L83-L84) only carries a vendor-autofilled BIC forward when the IBAN is *also* being carried; a valid image-stage IBAN drops the BIC, degrading non-CHF pain.001 output.

5. **🟠 Three whole files are dead** — `md_clean.py`, `test_ollama.py`, `rules/default.py` were all slated for deletion in the documented "Step 0" cleanup ([PLAN.md:20-23](PLAN.md#L20-L23)) but were never removed; `PROJECT_CONTEXT.md` even lists them as "removed." Plus `llm.extract_fields` (37 lines) is a dead, divergent re-implementation of the pipeline's merge.

**Recurring patterns:** `print(..., flush=True)` used as the logging mechanism everywhere (no `logging` module use despite a logging filter being installed); status/bank/mandatory-field string constants duplicated across modules instead of shared (the root enabler of bug #1); I/O calls (`fitz.open`, file writes, DB connects) lacking the try/except that the LLM-API calls do have; and a pervasive untyped `dict`-as-record style that hides key typos.

Total: **4 verified real bugs**, several medium correctness/robustness gaps, and a long tail of low-severity convention nits.

---

## Per-File Findings

### `app/main.py` — FastAPI app (382 lines)

- **Naming:** Clean overall. Minor: `w` for the `csv.DictWriter` at [main.py:372](app/main.py#L372) reads poorly for a non-loop object (prefer `writer`); one-letter `j`/`f`/`zf`/`buf` elsewhere are acceptable loop idioms.
- **Conventions:**
  - [main.py:36-44](app/main.py#L36-L44) **[MED]** Imports are split by executable code: stdlib imports at 18-25, then the `_SuppressPolling` class + `addFilter` (28-34), *then* third-party and local imports. PEP 8 wants all imports grouped at top (stdlib → third-party → local).
  - [main.py:30](app/main.py#L30) `filter(self, record)` lacks type hints, unlike the annotated helpers (`_debtor() -> dict`).
  - **[LOW]** Bare container hints (`-> list`, `-> dict`, `jobs: list`) instead of `list[dict]`; inconsistent docstring coverage across route handlers; section-divider comment lines exceed 100 cols (e.g. [main.py:177](app/main.py#L177) is 218 cols); mixed `'`/`"` quote styles.
  - **[LOW]** The `# ── Vendors ──` section header at [main.py:252](app/main.py#L252) also covers the unrelated `analytics` route (254-287) — misleading grouping.
- **Dead Code:** None within the file.
- **Other (error handling / smells):**
  - [main.py:161-174](app/main.py#L161-L174) **[MED]** `upload()` — `dest.write_bytes(await f.read())` and `pipeline.run_qr(...)` have no try/except; one bad file (disk error, unparseable PDF) aborts the whole multi-file batch with a 500 and leaves partial files.
  - [main.py:166](app/main.py#L166) **[MED]** `f.filename.lower()` assumes `filename` is non-None, but `UploadFile.filename` is Optional — a part without a filename raises `AttributeError` before the `.pdf` check.
  - [main.py:216-223](app/main.py#L216-L223) **[LOW]** `assign_bank` / `create_vendor` (297) / `update_vendor` (306): `await request.json()` is unguarded → malformed body yields a 500 instead of a clean 400. (`save_review` uses `request.form()`, which is safer.)
  - [main.py:98-111](app/main.py#L98-L111) **[LOW]** `_run_llm_batch` marks a job `'error'` on *any* exception (broad `except`), with no transient-vs-permanent distinction and no retry path — one API hiccup permanently errors the job.
- **🔒 Security (added — not flagged by the per-file pass):**
  - [main.py:169](app/main.py#L169) **[MED]** **Path traversal on upload.** `dest = UPLOAD_DIR / f"{job_id}_{f.filename}"` builds the path from the client-controlled multipart filename. A filename like `../../../var/www/x.pdf` escapes `UPLOAD_DIR` (the `{job_id}_` prefix only neutralizes the first `..` segment; enough `../` still escapes). The `.pdf` extension check is the only constraint. Sanitize with `os.path.basename(f.filename)` / reject separators. The same `{job_id}_*` glob pattern in `serve_pdf`/`delete_job` is lower-risk (path param won't contain `/`).
  - **[LOW]** No authentication on any endpoint, including `DELETE /api/clear-all` and `DELETE /api/jobs/{id}`. Acceptable only if the deployment is fully network-isolated; worth confirming.
- **Bugs:**
  - 🟠 ⚠️ **VERIFIED-BY-ME** [main.py:262](app/main.py#L262) — `qr_count`'s `(status = 'archived' AND match_type IS NULL)` never matches. `match_type` is `TEXT DEFAULT ''` ([db.py:22](app/db.py#L22)) and QR jobs never set it, so archived QR rows carry `''`, not NULL. `IS NULL` is always false here → commit `0221fbb`'s goal (count archived QR invoices) is **not achieved**. Fix: `(match_type IS NULL OR match_type = '')`.
  - 🟡 ⚠️ [main.py:362-363](app/main.py#L362-L363) — `download_confirm` exports/archives only `BKB`+`RAIFFEISEN` jobs; a `MANUAL`-routed job that passes `_export_blockers` is **silently omitted** from the zip with no operator warning (it does stay visible/non-archived, so not data loss). Consider surfacing a "not exported: N manual jobs" notice. *Low.*
  - ❌ ⚠️ [main.py:93-94](app/main.py#L93-L94) (re-queueing QR jobs) — **not a real bug**: `run_llm` sets `status` to `LLM-Done`/`needs_review` and it's in `PERSIST_KEYS`, so processed QR jobs leave `QR-processed` and aren't re-queued.
  - ❌ ⚠️ [main.py:189](app/main.py#L189) (stale `bank_target`) — **not a real bug**: the review form always submits `currency` (it's in `REVIEW_FIELDS`), and `bank_target` can only go stale if `currency` changes, which requires it to be submitted.

### `app/pipeline.py` — extraction orchestration (132 lines)

- **Naming:** Clean.
- **Conventions:** **[LOW]** Module constant `_MANDATORY` defined mid-file ([pipeline.py:35](app/pipeline.py#L35)) after `run_qr`; freeform single-line docstrings with no Args/Returns; bare `dict` params/returns (a `TypedDict` would catch the key typos that cause bug #1); 100+ col separator comments.
- **Dead Code:** None.
- **Other:**
  - [pipeline.py:45,58](app/pipeline.py#L45-L58) **[HIGH]** `run_llm` has **no try/except** around the LLM stages. The code relies on their `None`-return contract, but `extract_text_stage`/`extract_image_stage` can *raise* (`anthropic.APIError` subtypes are caught inside llm.py and converted to None — but `_make_client` raises `EnvironmentError` if the key is missing, and `fitz.open` raises on bad PDFs *before* any try block). An uncaught raise propagates instead of degrading to `needs_review`.
  - [pipeline.py:10](app/pipeline.py#L10) **[MED]** `run_qr` calls `qr_swiss.extract_from_pdf` (PDF I/O) with no error handling — a corrupt PDF crashes instead of falling back to `LLM-Pending`.
  - [pipeline.py:38-100](app/pipeline.py#L38-L100) **[MED]** `run_llm` is ~63 lines doing two-stage orchestration + interim vendor check + IBAN validation (twice) + match-type derivation + multi-source merge + IBAN/BIC carry-forward + source tagging + status. Split candidates: `_merge_fields`, `_finalize_iban`, `_derive_match_type`.
  - [pipeline.py:103-121](app/pipeline.py#L103-L121) **[LOW]** `run_vendor_check` both mutates the input dict in place *and* returns it; callers rely on each inconsistently — aliasing-bug risk.
- **Bugs:**
  - 🟠 ✅ **VERIFIED** [pipeline.py:61-62](app/pipeline.py#L61-L62) — When *both* stages return None, `match_type` is hard-coded `"image_only"`. This is reachable (no text layer + image API/parse failure), persisted (`match_type` ∈ `PERSIST_KEYS`), and **not re-run** (only `LLM-Pending`/QR jobs are reprocessed). The analytics query at [main.py:271](app/main.py#L271) (`match_type = 'image_only'`) then can't distinguish a genuine image-only success from a total failure, inflating the Image-Only count *and* double-counting the row in the `needs_review`/incomplete bucket. Fix: use a distinct failure/`none` marker, or exclude `needs_review` rows from the match_type counts.
  - 🟠 ✅ **VERIFIED** (med conf) [pipeline.py:83-84](app/pipeline.py#L83-L84) — Vendor-autofilled BIC is dropped when the image stage supplies a *valid* IBAN. The BIC copy is nested inside `if not merged.get("iban")`; once `merged["iban"]` is truthy the copy is skipped, and the final `run_vendor_check` ([pipeline.py:97](app/pipeline.py#L97)) takes the `if extracted_iban:` branch ([pipeline.py:110-115](app/pipeline.py#L110-L115)) that **never sets bic**. BIC feeds cross-border creditor agents in [xml_export.py:170-175](app/xml_export.py#L170-L175), so this degrades non-CHF payment files. Fix: copy the interim BIC independently of the IBAN gate.
  - 🟡 ⚠️ [pipeline.py:57,99](app/pipeline.py#L57-L99) — `needs_image` is decided on `interim` but final status on `merged`; in theory a complete `interim` could yield an incomplete `merged` (→ `needs_review` with no image fallback). Unverified; in practice `interim` and `merged` re-run the same vendor check, so they normally agree. *Low / edge.*
  - ❌ ✅ **REFUTED** [pipeline.py:48](app/pipeline.py#L48) (interim `is not None` vs `if v` truthiness mismatch) — benign: the LLM contract ([llm.py:13-14](app/llm.py#L13-L14)) mandates `null`, never `""`, so text fields are value-or-None and the two tests are equivalent; `needs_image` ([pipeline.py:57](app/pipeline.py#L57)) is the most permissive test and never under-triggers.

### `app/llm.py` — Claude Haiku extraction client (154 lines)

- **Naming:** **[LOW]** one-letter `m` (regex match, [llm.py:32](app/llm.py#L32)) and `b` (base64 string, [llm.py:62](app/llm.py#L62)).
- **Conventions:**
  - [llm.py:1](app/llm.py#L1) **[MED]** `import anthropic, base64, fitz, json, os, re` — multiple imports on one line, mixing stdlib and third-party. PEP 8 wants one per line, grouped.
  - [llm.py:3-4](app/llm.py#L3-L4) **[LOW]** multi-space `=` alignment (PEP 8 discourages); most helpers lack docstrings; [llm.py:146](app/llm.py#L146) `_pdf_to_images` is defined *below* its caller at [llm.py:61](app/llm.py#L61).
- **Dead Code:**
  - [llm.py:107-143](app/llm.py#L107-L143) **[MED]** `extract_fields` is unused — the pipeline calls `extract_text_stage`/`extract_image_stage` directly and reimplements the merge. `extract_fields` is referenced only in docstrings/comments. It is a divergent duplicate of pipeline logic.
  - [llm.py:22](app/llm.py#L22) **[LOW]** `_MANDATORY` is used *only* by the dead `extract_fields` → dead.
  - [llm.py:142](app/llm.py#L142) **[LOW]** `merged["_match_type"] = ...` writes a key nobody reads (pipeline uses its own local).
- **Other:**
  - [llm.py:25-29](app/llm.py#L25-L29), [146-153](app/llm.py#L146-L153) **[MED]** `_extract_text_layer` and `_pdf_to_images` call `fitz.open()` / `get_text()` / `get_pixmap()` with no try/except — a corrupt PDF raises out of the stage uncaught (unlike the API calls, which catch `APIError`→None). Note `_pdf_to_images` is called at [llm.py:61](app/llm.py#L61), *outside* the try block at [llm.py:69](app/llm.py#L69).
  - [llm.py:26-28,147-152](app/llm.py#L26-L28) **[LOW]** `doc.close()` without try/finally — handle leaks if an exception fires mid-use.
  - [llm.py:52,79](app/llm.py#L52-L79) **[LOW]** `response.content[0].text` assumes the first block is a text block (safe for Haiku 4.5 without extended thinking, but fragile).
- **Bugs:** Four claims raised; **all refuted** ✅ by independent verification:
  - ❌ [llm.py:75-79](app/llm.py#L75-L79) assistant-prefill — valid for `claude-haiku-4-5-20251001`; only breaks if `LLM_MODEL` is overridden to an Opus-4.6+/Sonnet-4.6 model, which doesn't exist in the repo. (Worth a one-line comment: "image stage requires a prefill-capable model.")
  - ❌ [llm.py:33-34](app/llm.py#L33-L34) greedy `\{.*\}` — fail-closed: worst case `json.loads` raises → None, never garbage data; greedy is the correct choice.
  - ❌ [llm.py:79](app/llm.py#L79) empty content → `raw="{"` → None — correctly handled.
  - ❌ [llm.py:139](app/llm.py#L139) hybrid merge — reachable only when both results are non-None; merge precedence (text non-null over image) is correct.

### `app/db.py` — SQLite DAL (187 lines)

- **Naming:** **[LOW]** one-letter `c` (currency, [db.py:79](app/db.py#L79)), `r` (rows, 59/128).
- **Conventions:** **[LOW]** plain vs `from` imports not separated; mixed `'`/`"` quotes; `get_db`/`init_db`/`upsert_job` missing docstrings/hints while peers have them; bare `list` return hints; stray triple blank line around [db.py:74-76](app/db.py#L74-L76).
- **Dead Code:** None.
- **Other:**
  - [db.py:70-73](app/db.py#L70-L73) **[MED]** `except sqlite3.OperationalError: pass` in the migration loop swallows *all* operational errors (disk full, malformed table), not just "duplicate column." Narrow it to the duplicate-column case.
  - [db.py:44-50](app/db.py#L44-L50) **[MED]** `get_db` commits on success but never `rollback()`s on exception — a partially-applied multi-statement op is silently discarded on close. Add `except: conn.rollback(); raise`.
  - [db.py:62](app/db.py#L62) **[LOW]** migration logging via `print(..., flush=True)` instead of `logging`.
- **Bugs:**
  - 🔴 ✅ **VERIFIED (high)** — **THE CRITICAL BUG.** [db.py:87-110](app/db.py#L87-L110) `upsert_job` interpolates caller kwargs keys directly into INSERT/UPDATE column lists with no whitelist (only `status` is validated). The illustrative typo is realized concretely: [pipeline.py:23](app/pipeline.py#L23) `run_qr` emits `"due_date": ""`, which is **not** a column in the jobs schema ([db.py:8-25](app/db.py#L8-L25); the migration block at [db.py:65-73](app/db.py#L65-L73) never adds it), and [main.py:171-172](app/main.py#L171-L172) passes `run_qr`'s output **unfiltered** via `**fields` (unlike the LLM/review callers, which filter through `PERSIST_KEYS`/`REVIEW_FIELDS`). The verifier ran it: **`sqlite3.OperationalError: table jobs has no column named due_date`, 0 rows written, exception propagates to a 500.** Every successful-QR upload fails on a DB without a legacy `due_date` column. *(Note: an existing production DB that once had the column would still work — so this manifests as a fresh-deploy/regression break.)* **Fix (pick one):** filter `run_qr` output to `PERSIST_KEYS` at [main.py:172](app/main.py#L172) (drop the dead `due_date` key); **or** add a column whitelist inside `upsert_job`; **or** restore the `due_date` column (it's also consumed by [xml_export.py `_earliest_date`](app/xml_export.py#L212), which currently always falls back to "today" because the date is never persisted).
  - ❌ ✅ **REFUTED** [db.py:169](app/db.py#L169) (`archive_jobs` placeholders) — the claim misread the line. It is `", ".join("?" * len(job_ids))`; `str.join` iterates the `"???"` string char-by-char → `"?, ?, ?"`. Verified valid SQL; archiving 2+ jobs works.
  - ❌ ✅ **REFUTED** [db.py:90-91](app/db.py#L90-L91) status validation, [db.py:104-105](app/db.py#L104-L105) empty-kwargs early return, [db.py:132-135](app/db.py#L132-L135) `get_jobs_by_status` — all confirmed correct/benign (the empty-kwargs guard also prevents malformed SQL).

### `app/qr_swiss.py` — Swiss QR-bill decoder (179 lines)

- **Naming:** **[LOW]** nested `g(i)` ([qr_swiss.py:131](app/qr_swiss.py#L131)) and the `_attempt = [0]` mutable-list counter ([qr_swiss.py:84](app/qr_swiss.py#L84)) — a `nonlocal int` would be idiomatic.
- **Conventions:** **[LOW]** stdlib `sys`/`pathlib` imported mid-function ([qr_swiss.py:79-80](app/qr_swiss.py#L79-L80)) rather than at top (the `fitz`/`pyzbar`/`PIL` local imports *are* justified as optional deps); nested helpers untyped; 100+ col debug-print lines.
- **Dead Code:** None.
- **Other:**
  - [qr_swiss.py:95-122](app/qr_swiss.py#L95-L122) **[HIGH]** `fitz.open`/`get_pixmap`/`Image.open` are unguarded (only the *import* catches `ImportError`). A corrupt/unrenderable PDF raises and breaks the documented "returns None" contract, crashing the caller — directly compounds the pipeline's missing error handling.
  - [qr_swiss.py:95-122](app/qr_swiss.py#L95-L122) **[MED]** `doc.close()` is not in try/finally — exception in the page loop leaks the document handle.
  - [qr_swiss.py:62-122](app/qr_swiss.py#L62-L122) **[LOW]** `_scan_pdf_qr` (~61 lines) mixes import, debug-save, multi-DPI scan, and crop logic; [qr_swiss.py:48-57](app/qr_swiss.py#L48-L57) zxingcpp fallback catches only `ImportError`, not runtime decode errors.
- **Bugs:** Both claims **refuted** ❌:
  - ❌ [qr_swiss.py:67-68](app/qr_swiss.py#L67-L68) docstring says "Bottom-half crop" but the crop is bottom-third/center-60% — docstring wording only, zero runtime effect (the full-page pass at every DPI runs first). *Fix the docstring.*
  - ❌ [qr_swiss.py:128](app/qr_swiss.py#L128) — no off-by-one; the `len(lines) < 30` guard plus `g()`'s internal bounds check make all reads safe.

### `app/xml_export.py` — pain.001 generator (219 lines)

- **Naming:** Clean.
- **Conventions:**
  - [xml_export.py:57](app/xml_export.py#L57) **[LOW]** `bank: str = None` should be `str | None` (the rest of the file uses PEP 604 unions, e.g. `_to_iso_date`).
  - [xml_export.py:50-54,159](app/xml_export.py#L50-L54) **[LOW]** `_sub`/`_add_tx`/`_parse_amount`/`_earliest_date` lack docstrings/full hints while `build_pain001`/`_get_service_level`/`_to_iso_date` have them.
- **Dead Code:** [xml_export.py:23](app/xml_export.py#L23) **[LOW]** stale comment `# Service levels removed — now determined by _get_service_level(...)` documents an absence; delete it.
- **Other:**
  - [xml_export.py:97,118](app/xml_export.py#L97-L118) **[MED]** Monetary sums (`ctrl_sum`, `pmt_sum`) accumulate **floats** then format with `:.2f`. Float drift between `CtrlSum` and the sum of individually-formatted `InstdAmt` values can be rejected by strict bank validators. Use `decimal.Decimal` for money.
  - [xml_export.py:195](app/xml_export.py#L195) **[MED]** `_parse_amount` does `str(s).replace(",", "")` — treats *every* comma as a thousands separator. European decimal commas (`"1234,50"`) become `123450.0` — a **100× error**. Given Swiss/EU invoices this input is plausible; there is no guard. (In practice amounts arriving from the LLM are decimal-point strings per the prompt, and QR amounts are formatted with `:.2f`, so the live risk is moderate — but operator-edited values flow through here too.)
  - [xml_export.py:57-156](app/xml_export.py#L57-L156) **[MED]** `build_pain001` is ~100 lines; extract the per-currency PmtInf block ([xml_export.py:117-152](app/xml_export.py#L117-L152)) into `_add_pmt_inf`.
- **Bugs:** Four claims, **all refuted** ❌ on reachability grounds:
  - ❌ [xml_export.py:31-47](app/xml_export.py#L31-L47) EUR→SEPA only under BKB — every EUR job is routed to `bank_target=BKB` by `derive_bank_target`, and `build_pain001` filters by bank, so EUR always hits the SEPA branch; the RAIFFEISEN/MANUAL EUR path is only reachable via a deliberate operator override.
  - ❌ [xml_export.py:129](app/xml_export.py#L129) `bank or "BKB"` fallback — only reachable from tests, which use CHF/EUR (both BKB).
  - ❌ [xml_export.py:143-144](app/xml_export.py#L143-L144) hardcoded `Ccy="CHF"` — matches the single fixed CHF debtor account; omitted (not mis-set) for other currencies.
  - ❌ [xml_export.py:175](app/xml_export.py#L175) creditor BIC truncated but debtor BIC not — debtor BIC is trusted operator config; `[:11]` isn't real validation anyway. *(Still worth `.strip()`-ing both for consistency.)*

### `app/vendors.py` — vendor CRUD (51 lines)

- **Naming:** Clean.
- **Conventions:** **[LOW]** `list_vendors() -> list` vs `lookup() -> dict | None` (inconsistent precision); `upsert_vendor`/`update_vendor`/`list_vendors`/`delete_vendor` lack docstrings while `lookup` has one.
- **Dead Code:** None.
- **Other:**
  - [vendors.py:34-39](app/vendors.py#L34-L39) **[MED]** `update_vendor` stores `receiver_name` **without** `.strip()`, whereas `upsert_vendor` (20,30) and `lookup` (11) all strip. A vendor updated with stray whitespace then fails the `lower(receiver_name) = lower(stripped)` match in `lookup`, **silently breaking IBAN lookups** for that vendor afterward. Strip on update too.
  - [vendors.py:8-13](app/vendors.py#L8-L13) **[LOW]** no error handling around DB access; the `vendors.iban NOT NULL` constraint makes an `IntegrityError`→raw-500 plausible.
- **Bugs:** Both claims **refuted** ❌ (genuine mechanisms, but not correctness defects per how the code is deployed):
  - ❌ [vendors.py:34-39](app/vendors.py#L34-L39) PUT to a nonexistent id returns `{"ok": true}` (0 rows updated) — missing-404 robustness nicety, idempotent-PUT-legal, UI always sources ids from a fresh GET. *(Consider checking `rowcount` for a real 404.)*
  - ❌ [vendors.py:16-31](app/vendors.py#L16-L31) `upsert_vendor` TOCTOU duplicate-insert — unreachable under the single-process, single-event-loop, no-`await`-inside deployment. *(A `UNIQUE` index on `lower(receiver_name)` would be sound hardening if you ever scale to multiple workers.)*

### `app/md_clean.py` — markdown table cleaner (106 lines) — ⚠️ DEAD FILE

- **Status:** **The entire module is unused.** `clean_markdown` is imported nowhere in the app (grep-confirmed; only referenced by `PLAN.md`/`Notes`), and [PLAN.md:21](PLAN.md#L21) + [PLAN.md:893](PLAN.md#L893) explicitly say "Delete `app/md_clean.py`" (Step 0). `PROJECT_CONTEXT.md:72` already lists it as "(removed)". → **Delete the file.**
- If kept: **[MED]** none of the five functions have docstrings; four lack type hints. **[LOW]** `is_sep_row`'s `re.match(r"^-*$", c)` ([md_clean.py:18](app/md_clean.py#L18)) matches an empty cell as a separator (`*` allows zero dashes — use `^-+$`); `process_block` (~50 lines, [md_clean.py:25-75](app/md_clean.py#L25-L75)) is multi-pass and could be split.
- **Bugs:** one claim, **refuted** ❌ (separator/data rows are consistently `len(active)` columns).

### `app/tests.py` — DEV_MODE startup self-tests (183 lines)

- **Naming:** **[LOW]** `BIC_PATTERN` is a function-local in UPPER_CASE ([tests.py:32](app/tests.py#L32)) — should be `bic_pattern`.
- **Conventions:** **[LOW]** imports scattered through the function body (deliberate for isolation, but obscures deps); several 120+ col lines; `check` helper untyped.
- **Dead Code:**
  - [tests.py:174-175](app/tests.py#L174-L175) **[MED]** `total = 33` (hand-maintained magic number) and `passed = total - len(failures)` — `passed` is **never used**; reporting is driven entirely by `len(failures)`. Remove both, or compute counts from data.
  - [tests.py:51](app/tests.py#L51) **[LOW]** `check("T4a-xml-parseable", True, "")` is a tautology that can never fail — no-op test.
- **Other:**
  - [tests.py:10](app/tests.py#L10) **[MED]** `run_startup_tests` is ~170 lines bundling 11 test groups + reporting. Split into per-concern functions (a real test framework — `pytest` — would be the better long-term move; the suite is also missing a `T3`).
  - [tests.py:120-121,152-169](app/tests.py#L120-L121) **[LOW]** `run_qr`/`upsert_vendor` calls aren't wrapped — a raise aborts the whole run instead of recording a failure (unlike the XML tests, which catch `ParseError`).
- **Bugs:** both claims **refuted** ❌ (`passed` is dead, so the `total=33` miscount is unobservable; `split("\n",1)[1]` is safe because `build_pain001` always embeds a literal `\n`). *Still worth fixing the dead `total`/`passed` accounting for clarity.*

### `app/test_ollama.py` — Ollama connectivity probe (102 lines) — ⚠️ OBSOLETE FILE

- **Status:** **Obsolete.** The app uses the Anthropic SDK (Claude Haiku), not Ollama; this utility is a leftover from the pre-rewrite era. [PLAN.md:23](PLAN.md#L23) + [PLAN.md:895](PLAN.md#L895) mark it "Delete" (Step 0). Imported nowhere. → **Delete the file.**
- If kept:
  - [test_ollama.py:15](app/test_ollama.py#L15) **[MED]** `from pathlib import Path` — **unused import**.
  - [test_ollama.py:21](app/test_ollama.py#L21) **[LOW]** `import socket` re-imported inside `get_host_ips` (already at module level, [test_ollama.py:14](app/test_ollama.py#L14)) — redundant.
  - [test_ollama.py:26](app/test_ollama.py#L26) **[MED]** **bare `except:`** swallows everything incl. `KeyboardInterrupt`/`SystemExit` — use `except OSError:`.
  - [test_ollama.py:81,85,89,90](app/test_ollama.py#L81-L90) **[LOW]** f-strings with no placeholders (drop the `f`).

### `rules/default.py` — extraction-rule template (10 lines) — ⚠️ STALE STUB

- **Status:** Comment-only stub referencing a `PATTERNS` dict and `extract.py` defaults. But `extract.py` was **deleted** in Step 0 ([PLAN.md:169](PLAN.md#L169)), `CSV_RULE_SET` is no longer read by any code (only docs mention it), and [PLAN.md:169](PLAN.md#L169) marks `app/rules/` for deletion. The file is an inert relic with stale references. → **Delete it** (and the `rules/` dir), or update the comment to point at the real extraction path if the template concept is still intended.

---

## Cross-File Issues

*(The dedicated cross-file agent was cut off by the session limit; the following is synthesized from a full read of all 11 files + grep.)*

1. **🟠 Duplicated mandatory-fields list (3 copies, divergent order).** `MANDATORY` at [main.py:58](app/main.py#L58), `_MANDATORY` at [pipeline.py:35](app/pipeline.py#L35), and `_MANDATORY` at [llm.py:22](app/llm.py#L22) all encode the same four fields — but in *different orders* and the llm.py copy is dead. Extract one shared constant (e.g. `db.MANDATORY_FIELDS`). This kind of scattered, hand-maintained schema knowledge is exactly what allowed the `due_date` bug.

2. **🟠 Status & bank-target strings hardcoded instead of shared.** `db.py` defines `STATUS_ENUM` ([db.py:39](app/db.py#L39)) but [main.py](app/main.py) and [pipeline.py](app/pipeline.py) use raw literals (`'QR-processed'`, `'LLM-Pending'`, `'needs_review'`, …) everywhere. Bank codes `'BKB'`/`'RAIFFEISEN'`/`'MANUAL'` are hardcoded in `derive_bank_target` *and* `set_bank_target` validation ([db.py:80-84,156](app/db.py#L80-L84)), in [xml_export.py:31-47](app/xml_export.py#L31-L47), and in [main.py:345-358](app/main.py#L345-L358). Promote to module-level constants/enums; the analytics `match_type` bug (#2 in Summary) is a direct symptom of this drift.

3. **🟠 Inconsistent logging — no `logging` module use.** Every module logs via `print(..., flush=True)`: [main.py:70,110](app/main.py#L70), [llm.py](app/llm.py) (8 sites), [db.py:62](app/db.py#L62) to stdout; [qr_swiss.py](app/qr_swiss.py) to **stderr**. Yet [main.py:28-34](app/main.py#L28-L34) installs a `logging.Filter` on `uvicorn.access` — so the logging framework is configured but never used by app code. No levels, no structured capture. Standardize on `logging.getLogger(__name__)`.

4. **🟠 Inconsistent error-handling contract.** LLM API calls catch and degrade to `None`; DB validation *raises* `ValueError`; but file/PDF I/O (`fitz.open`, `Image.open`, `dest.write_bytes`, `sqlite3.connect`) is **uncaught** across [pipeline.py](app/pipeline.py), [llm.py](app/llm.py), [qr_swiss.py](app/qr_swiss.py), [main.py](app/main.py). The pipeline assumes a "returns None on failure" contract that the I/O layers don't honor. Decide on one policy (degrade-to-None vs raise-and-handle-at-route) and apply it uniformly.

5. **🟠 Two parallel extraction implementations.** Dead `llm.extract_fields` ([llm.py:107-143](app/llm.py#L107-L143)) reimplements the same hybrid merge / match-type logic that `pipeline.run_llm` ([pipeline.py:67-99](app/pipeline.py#L67-L99)) implements live. They have already diverged (e.g. the `image_only`-on-total-failure bug exists only in the pipeline copy). Delete the dead one to remove the divergence trap.

6. **🟡 Untyped `dict`-as-record everywhere.** "Jobs" and "fields" are passed as bare `dict`s with stringly-typed keys through `main → pipeline → llm/qr → db → xml_export`. No `TypedDict`/dataclass. This is the systemic root cause of the `due_date` mismatch (#1) and makes key typos invisible until runtime. A single `JobFields` `TypedDict` would catch these statically.

7. **🟡 Import-style inconsistency.** [llm.py:1](app/llm.py#L1) uses a one-line multi-import; others use one-per-line. [main.py](app/main.py) interleaves imports with code. Local-inside-function imports appear in [qr_swiss.py](app/qr_swiss.py) (justified: optional deps) and [tests.py](app/tests.py) (justified: isolation) but also for plain stdlib (`sys`, `pathlib`) where it isn't.

8. **🟡 Stale documentation vs. reality.** `Notes/PROJECT_CONTEXT.md` and `README.md` still describe `extract.py`, `extract_fields`, and `CSV_RULE_SET` as live; `PROJECT_CONTEXT.md:72` lists `md_clean.py` as "removed" while the file exists. The "Step 0" deletions in `PLAN.md` were only partially executed. Reconcile docs with the actual tree.

---

## Recommendations (prioritized, highest impact first)

| # | Priority | Action | Where |
|---|----------|--------|-------|
| 1 | 🔴 **Critical** | Fix the `due_date` upload crash: filter `run_qr` output to `PERSIST_KEYS` at the call site **and** add a column whitelist in `upsert_job` (defense in depth). Decide whether `due_date` should be a real persisted column (export needs it). | [main.py:172](app/main.py#L172), [db.py:87-110](app/db.py#L87-L110), [pipeline.py:23](app/pipeline.py#L23) |
| 2 | 🟠 High | Fix analytics `qr_count`: `match_type IS NULL OR match_type = ''`. Verify dashboard counts after. | [main.py:262](app/main.py#L262) |
| 3 | 🟠 High | Stop mislabeling total extraction failure as `image_only`; use a `failed`/`none` marker or exclude `needs_review` from match_type counts. | [pipeline.py:62](app/pipeline.py#L62) |
| 4 | 🟠 High | Carry vendor BIC forward independently of the IBAN gate (or let the final vendor check set BIC when IBAN already present). | [pipeline.py:83-84](app/pipeline.py#L83-L84) |
| 5 | 🟠 High | Sanitize upload filenames (`os.path.basename`, reject path separators) to close the path-traversal write. | [main.py:169](app/main.py#L169) |
| 6 | 🟠 Med | **Delete the three dead files** (`md_clean.py`, `test_ollama.py`, `rules/default.py`) and the dead `llm.extract_fields`/`_MANDATORY`. Reconcile `Notes`/`README` with reality. | files above |
| 7 | 🟠 Med | Add error handling around PDF/file/DB I/O (try/except → degrade-to-None or 4xx) so a bad PDF or disk error doesn't 500 the batch; wrap `fitz` handles in try/finally. | [pipeline.py](app/pipeline.py), [llm.py:25-29,146-153](app/llm.py#L25-L29), [qr_swiss.py:95-122](app/qr_swiss.py#L95-L122), [main.py:161-174](app/main.py#L161-L174) |
| 8 | 🟠 Med | Use `decimal.Decimal` for monetary sums and fix `_parse_amount`'s comma handling (decimal vs thousands). | [xml_export.py:97,118,195](app/xml_export.py#L97) |
| 9 | 🟠 Med | `.strip()` `receiver_name` in `update_vendor`; narrow the migration `except` to duplicate-column; add `rollback()` to `get_db`. | [vendors.py:34-39](app/vendors.py#L34-L39), [db.py:70-73,44-50](app/db.py#L70-L73) |
| 10 | 🟡 Low | Centralize constants: one `MANDATORY_FIELDS`, reference `STATUS_ENUM` instead of literals, bank-code enum. | cross-file #1, #2 |
| 11 | 🟡 Low | Standardize on the `logging` module (replace `print(..., flush=True)`); guard `request.json()` in routes; introduce a `JobFields` `TypedDict`. | cross-file #3, #6 |
| 12 | 🟡 Low | Convention cleanup: import grouping, PEP 604 unions (`str \| None`), docstring/type-hint coverage, split long functions (`run_llm`, `build_pain001`, `run_startup_tests`), remove dead `total`/`passed` + tautological test, fix `qr_swiss` docstring. | per-file findings |

---

### Verification scorecard

| File | Bug claims | ✅ Verified real | ❌ Refuted | ⚠️ Unverified (my analysis) |
|------|-----------|----------------|-----------|------------------------------|
| main.py | 4 | — | — | 4 (1 real, 2 not-a-bug, 1 low) |
| pipeline.py | 4 | 2 | 1 | 1 (low edge) |
| llm.py | 4 | — | 4 | — |
| db.py | 5 | 1 (critical) | 4 | — |
| qr_swiss.py | 2 | — | 2 | — |
| xml_export.py | 4 | — | 4 | — |
| vendors.py | 2 | — | 2 | — |
| md_clean.py | 1 | — | 1 | — |
| tests.py | 2 | — | 2 | — |
| test_ollama.py / rules | 0 | — | — | — |

**Net: 4 verified real bugs (1 critical), plus the `main.py:262` analytics bug confirmed by my own static analysis** = 5 correctness defects to fix, led by the `due_date` upload crash.

---

# Frontend Review (HTML / CSS)

*(Continuation as requested. Reviewed inline — the workflow's agent pool was exhausted by the 2pm session limit. Scope: 3 templates + 2 CSS files; JS touched only where it confirms a dead-CSS claim or a security surface.)*

**Files:** [index.html](app/templates/index.html), [export.html](app/templates/export.html), [partials/modal_edit.html](app/templates/partials/modal_edit.html), [css/main.css](app/static/css/main.css) (active), [style.css](app/static/style.css) (orphaned).

## Frontend Summary

The active stylesheet ([main.css](app/static/css/main.css)) is a coherent dark-teal design system with sensible CSS-variable theming, and the markup is clean and semantic enough. But there are **two whole-file / large-block dead-code problems and significant CSS duplication**, plus a **cross-cutting stored-XSS risk** in how invoice/vendor data is rendered:

1. **🟠 `static/style.css` is entirely orphaned** — a *different* design system (light theme, `--lf-*` Lyfegen brand vars, `[data-theme="dark"]` support, classes like `.app-header`/`.theme-toggle`/`.review-modal`/`.badge-pending`/`.flag-pill`). Grep confirms **no HTML links it and no JS references it** (both templates link only `/static/css/main.css`). Dead.
2. **🟠 `export.html` re-implements ~200 lines of CSS inline** that already exist in `main.css`, with **conflicting values** — making large parts of `main.css`'s "EXPORT SCREEN" block dead/overridden.
3. **🟠 Stored-XSS surface:** invoice/vendor fields (receiver, filename, IBAN — sourced from uploaded PDFs, LLM extraction, and operator input) are rendered via `innerHTML` in the JS renderers without visible escaping.
4. **🟡 Accessibility gaps:** four modals with no `role="dialog"`/`aria-modal`/focus management; icon-only buttons without accessible names; sortable `<th onclick>` not keyboard-operable and missing `aria-sort`; drag-only export board.
5. **🟡 Heavy inline styles + inline event handlers** throughout, which hurt maintainability and force a CSP that allows `unsafe-inline`.

## Per-File Findings

### `app/templates/index.html` (147 lines)

- **[MED] Two modals styled entirely with inline `style="…"`** — `#vendors-modal` ([index.html:68-103](app/templates/index.html#L68-L103)) and `#analytics-modal` ([index.html:105-113](app/templates/index.html#L105-L113)) carry dozens of inline declarations instead of CSS classes. Unmaintainable and un-themeable; move to `main.css`.
- **[MED] Inline `<script>` in the page** ([index.html:118-144](app/templates/index.html#L118-L144)) — `showAnalytics` belongs in `dashboard.js` (or a dedicated file) with the other behavior; mixing it inline is inconsistent with the three external scripts loaded just above it.
- **[LOW] `showAnalytics` builds `body.innerHTML` from fetched data** ([index.html:134-139](app/templates/index.html#L134-L139)). The interpolated `${count}`/`${pct}` are server-computed *numbers*, so the live XSS risk is low — but the pattern is fragile; prefer `textContent`/DOM nodes.
- **[LOW] Inline event handlers everywhere** (`onclick`, `oninput`) — [index.html:24-30,39,48-54](app/templates/index.html#L24-L30). Couples behavior to markup and requires `unsafe-inline` in any CSP.
- **[LOW]** `class="topbar" id="upload-card"` ([index.html:11](app/templates/index.html#L11)) uses the **id** `upload-card`, but `main.css` only styles the **class** `.upload-card` — see dead-CSS note below; the id has no matching rule (drag styling comes from `body.drag-active::after`).

### `app/templates/export.html` (302 lines)

- **[MED] ~200-line inline `<style>` block** ([export.html:8-219](app/templates/export.html#L8-L219)) duplicates rules already in `main.css` (`.bank-col`, `.bank-header`, `.drop-zone`, `.invoice-card`, `.bottombar`, `.toast`, `.btn*`). Because the inline block follows the `<link>` to `main.css` ([export.html:7](app/templates/export.html#L7)), it **wins** — so `main.css`'s export-screen styles are largely overridden/dead for this page (see Cross-File).
- **[MED] Conflicting values between the inline copy and `main.css`:** the bank-column top borders are `#22BAA0` (teal) for both BKB and Raiffeisen inline ([export.html:49-50](app/templates/export.html#L49-L50)) vs `#3b82f6`/`#10b981` (blue/green) in `main.css` ([main.css:458-459](app/static/css/main.css#L458-L459)). The two screens disagree on the bank color language.
- **[MED] `<body>` has no class** ([export.html:221](app/templates/export.html#L221)) — `main.css` defines `body.export-page` ([main.css:431](app/static/css/main.css#L431)) and `.main.export-board` ([main.css:437](app/static/css/main.css#L437)), but this page sets neither, instead re-declaring `body`/`.main` inline ([export.html:9-23](app/templates/export.html#L9-L23)). Those `main.css` blocks are therefore dead.
- **[LOW]** Inline drag handlers (`ondragover`/`ondrop`/`ondragleave`) on each zone ([export.html:241,252,263](app/templates/export.html#L241)) — same CSP/maintainability note.
- **[LOW]** Viewport is `initial-scale=1.0` here vs `1` in index.html — trivial inconsistency.

### `app/templates/partials/modal_edit.html` (79 lines)

- Clean, class-driven (uses `main.css` classes), shared correctly between both pages via `{% include %}`. Good.
- **[MED] No dialog semantics** — `.modal-overlay`/`.modal` has no `role="dialog"`, `aria-modal="true"`, `aria-labelledby`, or focus trapping; the close `✕` ([modal_edit.html:6](app/templates/partials/modal_edit.html#L6)) has no `aria-label`. The bank-assignment pills ([modal_edit.html:64-66](app/templates/partials/modal_edit.html#L64-L66)) are clickable `<div>`s (not `<button>`), so they're not keyboard/AT-operable.
- **[LOW]** `<iframe id="pdf-frame">` correctly has a `title` ([modal_edit.html:11](app/templates/partials/modal_edit.html#L11)) — good.

### `app/static/css/main.css` (576 lines) — active stylesheet

- **Overall solid:** consistent variable system, scrollbar styling, status/IBAN-source chip taxonomy that mirrors the backend states. Good.
- **[MED] Dead / overridden blocks** (confirmed via grep — never applied because `export.html` ships its own inline copies and never sets the classes):
  - `body.export-page` ([main.css:431-436](app/static/css/main.css#L431-L436)) and `.main.export-board` ([main.css:437-442](app/static/css/main.css#L437-L442)) — no element uses these classes.
  - `.blocker-overlay` / `.blocker-box` / `.blocker-title` / `.blocker-list` ([main.css:554-575](app/static/css/main.css#L554-L575)) — the live blocker UI is `.blockers-popup`/`.blockers-box`/`.blocker-item` (defined inline in export.html and toggled by [export.js:217-221](app/static/js/export.js#L217-L221)). This older `.blocker-*` set is dead.
  - The whole export-screen duplication ([main.css:443-522](app/static/css/main.css#L443-L522)): `.bank-col`, `.bank-header`, `.drop-zone`, `.invoice-card`, `.bottombar`, etc. are shadowed by export.html's inline `<style>`.
  - `.upload-card`/`.upload-label`/`.upload-icon`/`.upload-title`/`.upload-hint`/`.upload-feedback` ([main.css:144-159](app/static/css/main.css#L144-L159)) — no `class="upload-card"` exists in the current HTML (the dashboard uses a compact topbar button); appears to be leftover from a previous big-drop-card design.
- **[LOW] Dead font references** — `:root` sets `font-family: 'Satoshi','Inter','Aptos',…` ([main.css:16](app/static/css/main.css#L16)), but only Inter is imported ([main.css:1](app/static/css/main.css#L1)) and `html,body` immediately overrides to `'Inter',system-ui` ([main.css:23](app/static/css/main.css#L23)). Satoshi/Aptos never load; the `:root` font-family is effectively dead.
- **[LOW] `@import url(google-fonts)` at the top** ([main.css:1](app/static/css/main.css#L1)) blocks rendering until fetched; a `<link rel="preconnect">` + `<link>` (or `@font-face` with `font-display:swap`) in the template `<head>` is faster.

### `app/static/style.css` (924 lines) — ⚠️ ORPHANED FILE

- **The entire file is dead.** It is a complete, *alternative* design system (light gradient theme, `--lf-*` Lyfegen brand palette, full `[data-theme="dark"]` dark-mode support, `.app-header`/`.app-title`/`.app-subtitle`/`.theme-toggle`/`.review-modal`/`.modal-pdf-pane`/`.modal-form-pane`/`.btn-secondary`/`.badge-pending`/`.badge-processing`/`.badge-mdx`/`.badge-llm`/`.flag-pill`/`.cell-notes`/`.upload-section`/`.form-fieldset` …) — **none of those classes appear in the current templates, no `<link>` points at `/static/style.css`, and no JS references it, `data-theme`, or `theme-toggle`.** → **Delete it**, *unless* this light/branded/dark-mode-capable theme is the intended future direction — in which case the decision to keep two divergent stylesheets should be explicit, and `main.css` reconciled against it. (Note: `style.css` even contains the only dark-mode + Lyfegen-brand implementation in the repo, so confirm it's truly abandoned before deleting.)

## Cross-File HTML/CSS Issues

1. **🟠 Two parallel stylesheets, one unused.** `style.css` (light/branded/themed) vs `main.css` (dark/teal) — entirely disjoint class vocabularies. Pick one source of truth; delete or merge the other. This is the frontend analogue of the dead `.py` files.
2. **🟠 Export-screen styles duplicated in three states** — `main.css` "EXPORT SCREEN" block, `export.html` inline `<style>`, and divergent values between them. Consolidate into `main.css` (give `export.html`'s `<body>` `class="export-page"` and the board `class="main export-board"`, then delete the inline block) so there is a single definition.
3. **🟡 Inline styles as a pattern** — beyond export.html's block, index.html styles two whole modals inline. Inline styles can't be cached, reused, or themed and bloat the HTML payload.
4. **🟡 Inline event handlers everywhere** (`onclick`/`oninput`/`ondrop`/`ondragover`) across all three templates. Combined with inline `<script>`, this prevents a strict Content-Security-Policy (you'd be forced to allow `unsafe-inline`). Move to `addEventListener` in the JS files.
5. **🟡 Color-language inconsistency** — bank colors differ between dashboard chips (`main.css` `.chip-bkb` blue, `.chip-raiff` green, [main.css:265-267](app/static/css/main.css#L265-L267)), the modal pills (`active-bkb` blue, `active-raiff` green, [main.css:415-417](app/static/css/main.css#L415-L417)), and the export board (teal-for-both inline). A user dragging between screens sees three different palettes for the same banks.

## Accessibility

- **[MED]** All four overlays (edit modal, `#vendors-modal`, `#analytics-modal`, `.blockers-popup`) lack `role="dialog"` + `aria-modal="true"` + labelling + focus trap + Esc handling. Keyboard/AT users can't operate or escape them reliably.
- **[MED]** Sortable headers are `<th onclick="sortTable(n)">` ([index.html:48-54](app/templates/index.html#L48-L54)) — not focusable, no `role`/`tabindex`, no `aria-sort` reflecting current order (the CSS only adds a visual `▲/▼`).
- **[MED]** Icon-only / glyph buttons have no accessible name: `×`/`✕` closes ([index.html:72,109](app/templates/index.html#L72), [modal_edit.html:6](app/templates/partials/modal_edit.html#L6)), `✦ Run AI`, `←`, `+ Upload`. Add `aria-label`.
- **[LOW]** The export board is drag-and-drop only with no keyboard path (the modal's bank pills are the only non-drag reassignment, and they're `<div>`s — see above). Provide a keyboard/click fallback.
- **[LOW]** `bank-pill` and `card-open-btn` interactive elements rely on hover-only affordances (`opacity:0` until `:hover`, [main.css:106,498](app/static/css/main.css#L106)); add `:focus-visible` states.

## Security

- **🟠 [MED] Stored-XSS surface via `innerHTML`.** Invoice and vendor records are rendered with `innerHTML` from fields that originate from **untrusted sources** — uploaded-PDF text, LLM extraction, operator free-text: [dashboard.js:114](app/static/js/dashboard.js#L114) (`buildRow` → receiver/filename/IBAN), [vendors.js:25,52](app/static/js/vendors.js#L25) (vendor receiver/IBAN/BIC), [export.js:51,210](app/static/js/export.js#L51) (invoice cards / blocker items). If any of these interpolate raw field values into the HTML string without escaping, a crafted PDF (e.g. a receiver name like `<img src=x onerror=…>`) or a malicious vendor entry yields **persistent XSS** that fires for the operator on every page load. **Verify these renderers HTML-escape all data values** (or switch to `textContent`/DOM construction). *(This bridges into the JS layer, but it's the single most important frontend finding, so flagging it explicitly.)*
- **[LOW]** Inline scripts/handlers (above) block adoption of a strict CSP, which would otherwise be the strongest mitigation for the XSS surface.

## Frontend Recommendations (prioritized)

| # | Priority | Action |
|---|----------|--------|
| F1 | 🟠 High | Audit/fix the `innerHTML` renderers ([dashboard.js:114](app/static/js/dashboard.js#L114), [vendors.js:52](app/static/js/vendors.js#L52), [export.js:51,210](app/static/js/export.js#L51)) to HTML-escape all invoice/vendor field values (or use `textContent`). Highest-risk frontend issue. |
| F2 | 🟠 Med | **Delete `static/style.css`** (or formally decide it's the target theme and reconcile). Removes 924 lines of dead/conflicting CSS. |
| F3 | 🟠 Med | De-duplicate the export-screen CSS: add `class="export-page"`/`export-board` to `export.html`, delete its inline `<style>`, and rely on `main.css`; then remove `main.css`'s now-truly-dead `.blocker-*` set. Single source of truth. |
| F4 | 🟡 Low | Add dialog a11y to all four modals (`role="dialog"`, `aria-modal`, labelling, focus trap, Esc); make sort headers `<button>`/`aria-sort`; add `aria-label` to icon buttons; make bank pills `<button>`. |
| F5 | 🟡 Low | Move inline `style="…"` (index.html modals) and inline `<script>` (`showAnalytics`) into `main.css` / a JS file; replace inline `on*` handlers with `addEventListener` to enable a strict CSP. |
| F6 | 🟡 Low | Unify the bank color language across chips / pills / export board; remove dead `:root` Satoshi/Aptos font names; consider `<link>`+`preconnect` over CSS `@import` for the font. |
