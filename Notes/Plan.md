# Plan — Per-Account Export Rework + pain.001.001.09 Migration

> Status: **IMPLEMENTATION COMPLETE** (T0–T11 done; T12–T14 trail docs). Source of truth for this work-stream.
> Related: [[DECISIONS#Per-Account Debtor Model (.env-driven)]] · [[DECISIONS#pain.001.001.09 Migration]] · [[DECISIONS#Vendor IBAN Takes Priority]] · [[PROJECT_CONTEXT]] · [[Features]]

---

## S — goal

C two banks, each holding multiple currency accounts, all defined in `.env`:

| Bank | Currencies (`*_CURRENCIES`) | Default acct | Specific accts |
|------|------------------------------|--------------|----------------|
| BKB | CHF, EUR, SEK | CHF | CHF, EUR |
| RAIFFEISEN | USD, CAD, GBP | USD | USD, CAD, GBP |

D resolution rule (the core mechanic):
```
bank = bank whose *_CURRENCIES contains ccy        (else MANUAL)
acct = accounts[bank].get(ccy)  or  accounts[bank][DEFAULT_CCY]
```
→ SEK ⇒ BKB, debits **CHF** IBAN. Unknown ccy ⇒ MANUAL, but operator may drag into a bank column → debits that bank's **default** acct.

N output upgraded `pain.001.001.03` → `pain.001.001.09`; each currency PmtInf block carries its own resolved DbtrAcct/DbtrAgt; cross-border (USD/CAD/GBP) carries structured creditor address + BICFI + ChrgBr.

---

## Reference docs

| Topic | Where | Note |
|-------|-------|------|
| **SIX CH XSD (in repo)** | [app/schemas/pain.001.001.09.ch.03.xsd](../app/schemas/pain.001.001.09.ch.03.xsd) | SIX Swiss-restricted, ©2021. **This is the validation target** — namespace = plain ISO `...pain.001.001.09`, root type `Document_pain001_ch` |
| ISO 20022 message def `pain.001.001.09` | https://www.iso20022.org/iso-20022-message-definitions | generic base the CH file restricts |
| SIX Validation Portal | https://validation.iso-payments.ch | runs **business rules** the XSD can't (presence, char-set) — manual cross-check |
| Current XML generator | [xml_export.py](../app/xml_export.py) | full rewrite target |
| Current routing | [db.py:94](../app/db.py#L94) `derive_bank_target` | becomes config-driven |
| Current single-debtor | [main.py:85](../app/main.py#L85) `_debtor()` | becomes `_accounts()` |

Key `.09` deltas vs `.03` (all silent-reject if missed) — **confirmed against the CH XSD**:
1. Namespace stays `urn:iso:std:iso:20022:tech:xsd:pain.001.001.09` (unchanged from generic .09); `xsi:schemaLocation` filename → `pain.001.001.09.ch.03.xsd`
2. FinInstn id element `BIC` → **`BICFI`** (`BICFIDec2014Identifier`) — Dbtr + Cdtr agents; `DbtrAgt` is **mandatory** ([xsd:1184](../app/schemas/pain.001.001.09.ch.03.xsd#L1184), [xsd:695](../app/schemas/pain.001.001.09.ch.03.xsd#L695))
3. `ReqdExctnDt` plain date → **mandatory** `DateAndDateTime2Choice`, wrap **`<ReqdExctnDt><Dt>…</Dt></ReqdExctnDt>`** ([xsd:1180](../app/schemas/pain.001.001.09.ch.03.xsd#L1180), [xsd:455](../app/schemas/pain.001.001.09.ch.03.xsd#L455))
4. **Creditor address is pure-structured** — `PostalAddress24_pain001_ch_5` ([xsd:1384](../app/schemas/pain.001.001.09.ch.03.xsd#L1384)) has **no `AdrLine`**; a free-text address blob is rejected. Must emit `StrtNm`/`PstCd`/`TwnNm`/`Ctry` (ISO 2-letter `CountryCode`)
5. `ChrgBr` is XSD-**optional** ([xsd:1188](../app/schemas/pain.001.001.09.ch.03.xsd#L1188)) — emit per business rule, schema won't force it

> N XSD enforces **structure/types, not presence** (address fields all `minOccurs=0`). Missing-country / missing-BIC for cross-border must be caught by our own blockers (T9) + the Validation Portal, not the XSD.

---

## Tasks

### T0 — Vendor IBAN priority + space-insensitive match
**Goal:** (A) matched vendor-table IBAN takes priority over LLM-read IBAN; (B) compare normalized (ignore spaces). LLM emits IBAN spaceless; frontend + vendor table **display** grouped-by-4 for readability; storage stays normalized.
- Compare/override logic: [pipeline.py:127-145](../app/pipeline.py#L127-L145) `run_vendor_check`
  - reuse normalization from [pipeline.py:148-155](../app/pipeline.py#L148-L155) `_validate_iban` → extract `_norm_iban(s)` = `re.sub(r"[^A-Za-z0-9]", "", s).upper()`
  - mismatch branch [pipeline.py:138-139](../app/pipeline.py#L138-L139): **override** `fields["iban"] = vendor["iban"]`, keep LLM value in `iban_mismatch_db` for audit, `iban_source="document_mismatch"`
- Vendor store/lookup: [vendors.py:4-13](../app/vendors.py#L4-L13), [vendors.py:16-31](../app/vendors.py#L16-L31) — normalize on store
- LLM-source tag (unchanged contract): [pipeline.py:116-117](../app/pipeline.py#L116-L117)
- Display (add `formatIban()` grouping-by-4):
  - dashboard cell: [dashboard.js:124](../app/static/js/dashboard.js#L124), chips [dashboard.js:146-152](../app/static/js/dashboard.js#L146-L152)
  - edit modal: [modal.js:67-94](../app/static/js/modal.js#L67-L94)
  - vendor table: [vendors.js:34](../app/static/js/vendors.js#L34), inputs [vendors.js:54](../app/static/js/vendors.js#L54)
- Tests: [tests.py:155-170](../app/tests.py#L155-L170) (T11x) — add space-insensitive + priority-override cases
- R financial: trusts curated table over per-invoice extraction → see [[DECISIONS#Vendor IBAN Takes Priority]]. Mismatch still surfaced (chip `doc ⚠`), status unchanged.

### T1 — Config loader (`app/config.py`, new) ✅ DONE
**Goal:** parse `.env` → `{accounts, currencies, defaults}` + ccy→bank index + `resolve_account(bank, ccy)`.
- Env key regex `^(?P<bank>[A-Z]+)_(?P<ccy>[A-Z]{3})_(IBAN|BIC)$`; plus `{BANK}_CURRENCIES`, `{BANK}_DEFAULT_CCY`, `DEBTOR_NAME`
- Model on existing env reads: [main.py:85-90](../app/main.py#L85-L90)
- Startup validation: IBAN MOD-97 on every configured acct (reuse `_validate_iban`); fail-fast log

### T2 — Config-driven routing ✅ DONE
**Goal:** `derive_bank_target(ccy)` reads ccy→bank index from T1 (drop hardcoded lists).
- [db.py:94-101](../app/db.py#L94-L101) — replace body; unknown ccy → `MANUAL`
- callers unaffected: [pipeline.py:27](../app/pipeline.py#L27), [pipeline.py:110](../app/pipeline.py#L110), [main.py:198](../app/main.py#L198)
- `set_bank_target` validation stays `BKB|RAIFFEISEN|MANUAL`: [db.py:173-180](../app/db.py#L173-L180)

### T3 — `.09` structural migration ✅ DONE
**Goal:** the breaking deltas (verified vs CH XSD).
- namespace consts + schemaLocation → `pain.001.001.09.ch.03.xsd`: [xml_export.py:21-23](../app/xml_export.py#L21-L23) ✅
- DbtrAgt BIC→BICFI: [xml_export.py:145-147](../app/xml_export.py#L145-L147) (DbtrAgt mandatory ⇒ acct BIC required) ✅
- CdtrAgt BIC→BICFI: [xml_export.py:169-174](../app/xml_export.py#L169-L174) ✅
- ReqdExctnDt wrap `<Dt>`: [xml_export.py:134](../app/xml_export.py#L134) ✅
- **Done note:** structural rename only — single-debtor signature unchanged (per-account = T4, structured Cdtr addr + ChrgBr = T5). Existing tests' ns strings bumped `.03→.09` ([tests.py](../app/tests.py)). Added **guarded** XSD-validation check `Txsd-*` (skips if `xmlschema` absent → activates with T10); `build_pain001` CHF + multi-ccy output confirmed **XSD-valid** vs the SIX CH XSD. Residual: empty debtor BICFI still emits an invalid `<BICFI/>` — resolved by T4 (per-account, BIC required) + T9 export gating.

### T4 — Per-account DbtrAcct/DbtrAgt ✅ DONE
**Goal:** `build_pain001(jobs, accounts, bank)`; each ccy PmtInf uses `resolve_account(bank, ccy)`; DbtrAcct carries acct `Ccy`.
- signature + debtor block: [xml_export.py:56-156](../app/xml_export.py#L56-L156) (esp. [137-147](../app/xml_export.py#L137-L147)) ✅
- `_get_service_level` keep, drive from config bank: [xml_export.py:25-46](../app/xml_export.py#L25-L46) ✅ (unchanged; driven by `bank`)
- N PmtInf still grouped per **transaction** ccy (clean per-ccy `CtrlSum`); SEK + CHF blocks may both debit the BKB-CHF IBAN; DbtrAcct/Ccy = account ccy, tx InstdAmt/Ccy = payment ccy (FX). Verify against XSD/bank.
- **Done note:** `build_pain001(jobs, accounts, bank)` — `accounts` is the config dict (`config.get_accounts()`), supplying `debtor_name` + resolution. Per ccy: `acct = config.resolve_account(bank, ccy, accounts)` → `{iban, bic, ccy}`; emits `DbtrAcct/IBAN`, `DbtrAcct/Ccy`=account ccy, `DbtrAgt/FinInstnId/BICFI`=acct bic. Raises `ValueError` if a payable (bank,ccy) has no IBAN+BIC (defensive; **T9** gates upstream). **Enabler:** `config.resolve_account` gained `cfg=None` (resolve against passed config → `build_pain001` stays pure) and now returns the resolved account `ccy`. Verified: SEK→BKB-CHF IBAN w/ `Ccy=CHF` + `InstdAmt Ccy=SEK` (FX), EUR→own EUR acct/SEPA; CHF+EUR+SEK output **XSD-valid** vs SIX CH XSD. Closes T3's empty-`<BICFI/>` residual. **Out-of-scope deferred:** main.py download route (`build_pain001(..., debtor, ...)`) still passes the legacy single-debtor dict → **T8** must switch it to `config.get_accounts()` (export will 500 until then). Existing XML startup tests (T4/T5/T6/Txsd) adapted to the new signature; full per-account/SEK assertion suite is **T11**.

### T5 — Cross-border conformance (structured address + ChrgBr) ✅ DONE
**Goal:** **pure-structured** `Cdtr/PstlAdr` (`StrtNm`,`BldgNb`,`PstCd`,`TwnNm`,`Ctry`) — **no `AdrLine`** (CH creditor profile `_ch_5` forbids it) + `ChrgBr` (`SLEV` SEPA / `SHAR` SWIFT).
- emit in `_add_tx`: [xml_export.py:158-190](../app/xml_export.py#L158-L190)
- structured profile ref: [xsd:1384](../app/schemas/pain.001.001.09.ch.03.xsd#L1384); `Ctry` = ISO 2-letter `CountryCode`
- ChrgBr placement: PmtInf level, derived from service level [xml_export.py:126-133](../app/xml_export.py#L126-L133); XSD-optional ([xsd:1188](../app/schemas/pain.001.001.09.ch.03.xsd#L1188))
- **Done note:** `_cdtr_address(cdtr, job)` — pure-structured helper; emits `PstlAdr` only when at least one `cdtr_*` field present; XSD element order matches `PostalAddress24_pain001_ch_3` sequence (`StrtNm`→`BldgNb`→`PstCd`→`TwnNm`→`Ctry`). `ChrgBr` inserted after `DbtrAgt` at PmtInf level: `SEPA`→`SLEV`, `add_swift`→`SHAR`, CHF domestic omitted. Tests T5b–T5f assert all three rules + XSD validity confirmed (Txsd). **XSD note:** Plan cited `PostalAddress24_pain001_ch_5` (no AdrLine) as the Cdtr profile, but the actual binding is `PostalAddress24_pain001_ch_3` (AdrLine `minOccurs=0` — technically allowed); design intent (structured only) is unchanged. **Out-of-scope deferred:** `cdtr_*` fields are not yet in the DB schema or LLM extraction → `_cdtr_address` will silently emit nothing until T6/T7 land (correct behaviour). Txsd validated CHF + EUR multi-ccy output; EUR now carries `ChrgBr=SLEV`.

### T6 — LLM extracts creditor address (structured) ✅ DONE
**Goal:** extract address as **discrete components** (not a blob — the CH creditor profile has no AdrLine to dump free-text into).
- prompt + JSON contract: [llm.py](../app/llm.py) extract stages; contract documented [DECISIONS.md:40-45](DECISIONS.md#L40)
- new keys: `cdtr_street, cdtr_building_no, cdtr_postcode, cdtr_town, cdtr_country` (ISO-2 country; null-safe)
- **Done note:** `PROMPT` in `app/llm.py` updated — all 5 `cdtr_*` keys added to the fields list and the example JSON; `cdtr_country` rule: 2-letter ISO code or null, never a full name. Fields flow through the pipeline merge dict but are dropped by `PERSIST_KEYS` in `main.py:52-57` until T7 adds them to `_JOB_COLUMNS` and `PERSIST_KEYS`. Startup tests: all 38 checks pass.

### T7 — DB columns + migration ✅ DONE
**Goal:** persist address fields (`cdtr_street, cdtr_building_no, cdtr_postcode, cdtr_town, cdtr_country`).
- add to schema + `_JOB_COLUMNS`: [db.py:20-21](../app/db.py#L20-L21), [db.py:46-47](../app/db.py#L46-L47) ✅
- ALTER-TABLE migration (follow existing pattern): [db.py:79-80](../app/db.py#L79-L80) ✅
- review-form whitelist: [main.py:54-55](../app/main.py#L54-L55) ✅
- **Done note:** 5 `cdtr_*` columns added to `SCHEMA` (CREATE TABLE) and `_JOB_COLUMNS` (`db.py`); 5 `ALTER TABLE jobs ADD COLUMN` migration statements added (try/except `duplicate column name` pattern, safe on existing DBs). `PERSIST_KEYS` in `main.py` extended so pipeline output writes the fields; `REVIEW_FIELDS` extended so operators can correct addresses via the review form. `_cdtr_address` in `xml_export.py` (T5) was already reading from job dicts — now those keys are actually persisted. Startup tests: all checks pass. **Residual:** existing job rows get empty strings for all `cdtr_*` — cross-border jobs created before this migration lack address data → `_cdtr_address` emits nothing → expect `needs_review`-equivalent gap until re-extracted or manually filled via review form. Frontend edit modal does **not** yet expose `cdtr_*` fields (out of scope; belongs to T13 or a dedicated UI task).

### T8 — Wire accounts into routes ✅ DONE
**Goal:** `_debtor()` → `_accounts()`; pass to generator.
- [main.py:85-90](../app/main.py#L85-L90) → load via T1
- download: [main.py:389-408](../app/main.py#L389-L408)
- **Done note:** `_debtor()` removed; `_accounts()` added (one-liner: `return config.get_accounts()`). `import config` added. Download route (`POST /download/confirm`): `debtor = _debtor()` → `accounts = _accounts()`; both `build_pain001` calls now receive the full accounts config dict. The export 500 noted in T4's done-note is resolved. Startup routing tests (T7a/T7b/T8c) were already failing pre-T8 due to missing env vars (config-driven routing since T2 requires `BKB_CURRENCIES`/`RAIFFEISEN_CURRENCIES` to be set); not a regression.

### T9 — Export gating (greyed-out + account resolvability) ✅ DONE
**Goal:** not-export-ready rows greyed/undraggable; block if a payable (bank,ccy) resolves to no acct; unknown-ccy draggable into bank → default acct (falls out of T1 resolver).
- blockers: [main.py:117](../app/main.py#L117) `_export_blockers`, readiness [main.py:367](../app/main.py#L367)
- add blocker: `(bank,ccy)` unresolvable; cross-border missing BIC/address
- frontend grey/disable cards by status: [export.js:12-14](../app/static/js/export.js#L12-L14), render [export.js:33-34](../app/static/js/export.js#L33-L34), drop zones [export.html:234-272](../app/templates/export.html#L234-L272)
- existing hard-block rule (do not regress): [[DECISIONS#Export Hard-Blocked on Incomplete Invoices]]
- **Done note:** `_export_blockers()` extended with three-rule gate: rule 1 = original incomplete/bad-status check (unchanged, `blocker_type: incomplete`); rule 2 = `(bank,ccy)` resolves to no IBAN+BIC via `config.resolve_account` (`blocker_type: unresolvable_account`); rule 3 = RAIFFEISEN job missing creditor `bic` or `cdtr_country` (`blocker_type: cross_border_incomplete`). Each entry carries `blocker_type` (backwards-compat). `export.js`: `blockedIds` Set populated by `fetchBlockedIds()` on init + after each drag+drop; `makeCard` locks (greyed, undraggable, distinct tooltip) when `blockedIds.has(job.id)` or `needs_review`; `showBlockersPopup` uses `_blockerReasons()` for type-aware messages. MANUAL cards stay draggable (unknown-ccy drag → default account via T1). No HTML changes needed — existing `.locked` CSS applies. Startup tests: all checks pass.

### T10 — Deps + XSD (XSD already in repo) ✅ DONE
**Goal:** XSD validation available in container.
- add `xmlschema` (pure-python, no libxml build) to `requirements.txt` **and** `Dockerfile` — see [[feedback_silent_imports]] ✅
- XSD already shipped: [app/schemas/pain.001.001.09.ch.03.xsd](../app/schemas/pain.001.001.09.ch.03.xsd) (SIX CH ©2021) — ensure `COPY app/schemas` in Dockerfile / not `.dockerignore`d ✅

**Done note:** `xmlschema>=2.4.0` added to `requirements.txt` (pure-Python, no libxml build dep). `Dockerfile`: explicit `COPY app/schemas /app/schemas` inserted before `COPY app/ .` to ensure SIX CH XSD lands in container. No `.dockerignore` excludes schemas. Startup tests pass.

### T11 — Tests incl. XSD validation ✅ DONE
**Goal:** assert `.09` shape + validate output against XSD.
- build_pain001 tests: [tests.py:43-103](../app/tests.py#L43-L103) ✅
- routing tests: [tests.py:104-107](../app/tests.py#L104-L107) ✅ (CHF→BKB, USD→RAIFFEISEN config-derived)
- namespace: [tests.py:65,88,99,109,117,143-144,158-159,174-175](../app/tests.py#L65) — `urn:iso:std:iso:20022:tech:xsd:pain.001.001.09` ✅
- BICFI: [tests.py:176,145-148](../app/tests.py#L176) — agents use BICFI not BIC ✅
- ReqdExctnDt/Dt: [tests.py:162-163](../app/xml_export.py#L162) wraps date in `<Dt>` ✅
- SEK→CHF-acct resolution: [tests.py:329-331](../app/tests.py#L329) fallback to default ✅
- per-acct debtor: [tests.py:48-58,62,137-142,157](../app/tests.py#L48) — each ccy resolves via config ✅
- **XSD-valid output**: [tests.py:338-361](../app/tests.py#L338) — Txsd validates CHF+multi-ccy vs SIX CH XSD ✅
- **T0 vendor cases**: [tests.py:246-274](../app/tests.py#L246) — exact match, mismatch (vendor wins, audit), autofill, no-entry, space-insensitive ✅

**Done note:** 38 startup checks pass. T0 complete: `run_vendor_check` (pipeline.py:127-147) normalizes IBAN via `_norm_iban` (strip spaces/non-alphanumeric, upper), vendor table wins on mismatch (extracted in `iban_mismatch_db` audit trail), frontend displays formatted-by-4 (dashboard.js:3-5, modal.js:5-6, vendors.js:3-4). T3 deltas confirmed: namespace `.09`, BICFI on Dbtr+Cdtr agents (xml_export.py:176), ReqdExctnDt→`<Dt>` wrapper (xml_export.py:162-163), structured PstlAdr (xml_export.py:195-228, no AdrLine). Txsd: CHF domestic + EUR multi-ccy output **passes SIX CH XSD validation** (tests.py:355-361, guarded—xmlschema optional).

### T12 — `.env.example`
**Goal:** new keys; drop `DEBTOR_IBAN`/`DEBTOR_BIC` (keep `DEBTOR_NAME`). (file edit blocked by sandbox policy → operator applies, mirror in [PROJECT_CONTEXT.md:215-244](PROJECT_CONTEXT.md#L215))

### T13 — Frontend polish (optional)
**Goal:** per-currency sub-totals so operator sees which acct each block draws.
- totals render: [export.js:122-147](../app/static/js/export.js#L122-L147)

### T14 — Doc sync
**Goal:** keep notes current (global rule).
- [PROJECT_CONTEXT.md](PROJECT_CONTEXT.md) — env vars, schema, routing table, pipeline diagram
- [Features.md](Features.md) — export feature requirement change
- [DECISIONS.md](DECISIONS.md) — entries added (this commit)

---

## Recommended model (mode) + effort per step

> "mode" = model tier. Haiku has no settable reasoning effort (`—`). Effort scale: low / med / high.
> Default permission mode: `plan` for design-bearing steps (T3/T4/T5), `acceptEdits` for mechanical, `default` otherwise.

| Step | Model | Effort | Perm mode | Rationale |
|------|-------|--------|-----------|-----------|
| T0 Vendor priority/match | **Opus** | high | default | financial-correctness; silent IBAN override risk |
| T1 Config loader | Sonnet | med | default | parsing + validation, moderate logic |
| T2 Routing | Haiku | — | acceptEdits | trivial body swap |
| T3 `.09` migration | **Opus** | high | plan | schema-breaking; reject-on-error |
| T4 Per-account debtor | **Opus** | high | plan | core mechanic; FX/CtrlSum subtlety |
| T5 Address + ChrgBr | Sonnet | high | plan | conformance detail, IG-sensitive |
| T6 LLM address fields | Sonnet | med | default | prompt + schema change |
| T7 DB columns + migration | Sonnet | med | default | migrations carry data risk |
| T8 Route wiring | Sonnet | low | acceptEdits | plumbing |
| T9 Export gating | Sonnet | med | default | blocker logic + UI state |
| T10 Deps + XSD | Haiku | — | acceptEdits | file adds, mechanical |
| T11 Tests + XSD | **Opus** | high | default | tests define correctness contract |
| T12 `.env.example` | Haiku | — | acceptEdits | config text |
| T13 Frontend subtotals | Sonnet | med | default | UI, low risk |
| T14 Doc sync | Haiku | — | acceptEdits | prose |

Dependency order: **T1 → {T2, T3, T4} → T5 → T6/T7 → T8 → T9 → T10/T11**. T0 independent (do first or parallel). T12–T14 trail implementation.

---

## R — gaps still open for full pipeline

1. ~~SIX vs generic XSD~~ **RESOLVED** — SIX CH XSD `pain.001.001.09.ch.03` is in repo and is the validation target. Residual: it enforces *structure not presence* + a restricted **char-set** (Latin subset) → still cross-check via the SIX Validation Portal.
2. **Address backfill** — new `cdtr_*` columns empty on existing jobs → every pre-migration cross-border job → `needs_review`. Expect a review spike on first post-migration run.
3. **Char-set restriction** — Swiss IG limits to a Latin subset; LLM may emit accented/non-Latin chars → add a sanitizer before XML emit (gap, not yet tasked).
4. **requirements.txt + Dockerfile** — `xmlschema` must land in **both** or validation silently no-ops in container ([[feedback_silent_imports]]).
5. **ChrgBr semantics** — confirm SEPA=`SLEV`, SWIFT=`SHAR`/`DEBT` against the bank's IG; wrong charge bearer can change who pays fees.
6. **MANUAL still excluded** from auto-routing; genuinely unknown ccy stays MANUAL until operator drags it. Greyed not-ready rows cannot export → prevents corrupted XML.
7. **Concurrency** — `/api/run-llm-batch` double-trigger ([DECISIONS.md:240](DECISIONS.md#L240)) unrelated but still open.

## P — done criteria
- pytest green incl. XSD-validate
- BKB file: CHF + SEK blocks both debit BKB-CHF IBAN; EUR block debits BKB-EUR IBAN
- Raiffeisen file: USD/CAD/GBP debit own acct, fallback → USD
- cross-border tx carry BICFI + structured PstlAdr + ChrgBr
- vendor-matched job: table IBAN wins over LLM, mismatch surfaced, spaces ignored in compare, displayed grouped-by-4
