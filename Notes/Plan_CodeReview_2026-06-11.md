# Code Review — Remediation Plan (2026-06-11)

High-effort review of the `desktop` branch diff (`master...HEAD` + uncommitted
`xml_export.py`/`modal.js`) plus new `scripts/` + `docs/`. User priority: **dead
code + security**. Findings ranked; each load-bearing claim verified against source.

Context: app is currently exposed to testers via the Entra-tenant-gated dev tunnel
(`lyfegen-invoice-test`), **desktop mode on, `APP_PASSWORD` off** — so the settings
write surface is reachable by every tenant member. See [[DECISIONS#Testing-Phase Access — Microsoft Dev Tunnels (branch: desktop, 2026-06-11)]].

Severity: 🔴 critical · 🟠 high · 🟡 medium · ⚪ low/cleanup.

---

## Security

### 🔴 1. Env-file injection via `debtor_name` / `llm_model`
- **Where:** [app/main.py:468](../app/main.py#L468) (`_parse_settings_payload`) + [app/settings_store.py:89](../app/settings_store.py#L89) (`set_many`).
- **Bug:** only `api_key` is whitespace-checked; `debtor_name`/`llm_model` are merely
  `.strip()`'d. `.strip()` does NOT remove **interior** newlines, and `set_many`
  writes the value raw as `f"{key}={value}"`.
- **Exploit:** `POST /api/settings` with
  `debtor_name = "Lyfegen\nANTHROPIC_API_KEY=sk-attacker\nAPP_PASSWORD=mine"` →
  three lines land in `%APPDATA%\InvoiceProcessor\settings.env`. Next launch,
  `load_into_environ()` (`setdefault`) injects any key not already set
  (`DEV_MODE`, `APP_PASSWORD`, key-if-unset).
- **Fix:** reject `\r`/`\n` in every persisted value (validate in
  `_parse_settings_payload` AND defensively in `set_many`).

### 🟠 2. `POST`/`GET /api/settings` gated by deployment-mode, not authorization
- **Where:** [app/main.py:539](../app/main.py#L539) (`if not paths.is_desktop(): 403`).
- **Bug:** deployment-mode check ≠ authz. In the tunnel config every tenant member
  can **read all debtor IBAN/BIC + payee** (`GET`) and **overwrite any
  `{BANK}_{CCY}_IBAN`** with an attacker IBAN passing MOD-97 (`POST`) → silent
  payment redirection on next export; can also replace the API key.
- **Fix (test phase, now):** set `APP_PASSWORD` — the auth layer already exists
  ([app/auth.py](../app/auth.py)) and wraps all non-static routes. **Decision needed**
  from owner before deeper rework (real identity/role gate for production).

### 🟠 3. `GET /api/pdf/{job_id}` glob injection → cross-tester file read (IDOR)
- **Where:** [app/main.py:293](../app/main.py#L293); same pattern in DELETE
  [main.py:301](../app/main.py#L301) and clear-all [main.py:311](../app/main.py#L311).
- **Bug:** `job_id: str` interpolated unescaped into `UPLOAD_DIR.glob(f"{job_id}_*")`.
  `/api/pdf/*` (or `%2A`) → pattern `*_*` → serves the first stored PDF; one tester
  reads another's invoice on the shared instance.
- **Fix:** type `job_id` as `int` on the route, or `glob.escape(job_id)`.

---

## Correctness

### 🟠 4. Creditor IBAN emitted with weak normalization (money path)
- **Where:** [app/xml_export.py:249](../app/xml_export.py#L249) (uncommitted change).
- **Bug:** `job.get("iban","").replace(" ","")` strips ASCII spaces only;
  `config._norm_iban` (already importable — `xml_export` imports `config`) strips
  all non-alphanumerics + uppercases. Creditor IBAN comes from the review form and
  is never MOD-97-validated server-side → a non-breaking space (common in PDF text),
  dash, or lowercase flows into `<CdtrAcct><IBAN>` malformed → bank rejects the whole
  pain.001 batch while the job shows green in the UI.
- **Fix:** `_sub(cdtr_id, "IBAN", config._norm_iban(job.get("iban", "")))`.

### 🟡 5. `DEV_MODE=true` bricks the desktop build; template still advertises it
- **Where:** [desktop/settings.env.template:34](../desktop/settings.env.template#L34) +
  lifespan `import tests` in [app/main.py](../app/main.py) (~L76).
- **Bug:** spec excludes the `tests` module (`excludes=["pyzbar","tests","pytest"]`)
  and the `except` re-raises. A tester who uncomments `DEV_MODE` → ImportError on
  launch → startup aborts, no UI to fix the file.
- **Fix:** drop the knob from the template, or make the lifespan import failure
  non-fatal when `sys.frozen`.

### 🟡 6. `save_settings` cannot unset model/key; stale-removal covers only bank keys
- **Where:** [app/main.py:558-576](../app/main.py#L558).
- **Bug:** `LLM_MODEL`/`LLM_MODEL_TEXT`/`ANTHROPIC_API_KEY` written only when
  non-empty; `stale` = `_bank_env_keys(...)` only. → blanking the model field is a
  silent no-op (old value persists), and `LLM_MODEL_TEXT` is force-overwritten with
  the image model every save. "Replace all config" contract holds only for bank keys.
- **Fix:** include the non-bank keys in the removable set when their field is blank;
  stop mirroring `LLM_MODEL`→`LLM_MODEL_TEXT` unconditionally (or expose both).

---

## Dead code (user priority)

### ⚪ 7. `settings_store.set_value()` — zero callers
- **Where:** [app/settings_store.py:65](../app/settings_store.py#L65).
- Grep: only the definition + its docstring + a PROJECT_CONTEXT mention. Was the
  persistence call of the deleted `POST /api/settings/api-key`; all writes now go
  through `set_many`. **Fix:** delete the function + its docstring sentence.

### ⚪ 8. `xml_export.py:178` debtor IBAN `.replace(" ","")` — redundant
- **Where:** [app/xml_export.py:178](../app/xml_export.py#L178) (uncommitted).
- `accounts` comes from `config.load_accounts()`, which already `_norm_iban`s every
  account IBAN; the save path strips again. The `.replace` can never change input.
  **Fix:** revert line 178; keep the (strengthened) fix on 249 — see #4.

### ⚪ 9. `start-test-tunnel.ps1` — hardcoded URL + port not pinned
- **Where:** [scripts/start-test-tunnel.ps1](../scripts/start-test-tunnel.ps1) (~L33, ~L88).
- (a) Banner hardcodes `x3m2th39-8743.euw.devtunnels.ms` — rots on tunnel recreate
  (real URL prints below anyway). (b) `$Port=8743` never passed to the launcher, so
  if 8743 is busy `launcher._pick_port()` moves the app to an ephemeral port while
  the tunnel still targets 8743 → testers hit a dead endpoint, script reports success.
- **Fix:** `$env:INVOICE_PORT=$Port` before launch; drop the hardcoded URL line;
  check the probe response body (mirror `launcher._is_our_server`) before "reusing".

---

## Efficiency

### ⚪ 10. `qr_swiss.py` retries the failed `pyzbar` import per PDF
- **Where:** [app/qr_swiss.py](../app/qr_swiss.py) (~L82).
- Import sits in the per-job scan body. Python caches successful imports, not
  failures → in the desktop build (no zbar DLL, the target case) every invoice
  re-runs the finder scan + DLL-load attempt before failing.
- **Fix:** resolve once at module load into a `_ZBAR = None` sentinel; pass it down
  (`_decode_spc` already accepts `None`).

---

## Honorable mentions (cut for the 10-cap, real but minor)
- 4th copy of the HTML-escape helper (`escS` in settings.js vs `esc` in
  dashboard/export/vendors) — diverging; settings.js interpolates user bank names
  into `value="..."` without escaping `'`. Consolidate into `banks.js`.
- `checkFirstRun()` fires `/api/settings/status` on every load, incl. server mode.
- Stale-key removal pops **shell-provided** env vars in a live `INVOICE_DESKTOP=1`
  dev session.
- Bundle ships `app/schemas/` though nothing reads it in the frozen app.
- Possibly-stale `.col-bkb`/`.col-raiff` CSS (unverified).

---

## Suggested order
1. **#1, #4** — apply now (low-risk, no behavior change to intended flows).
2. **#2** — set `APP_PASSWORD` on the tunnel today; owner decides production authz.
3. **#3** — `int`-type the id routes.
4. **#7, #8, #9** — cleanup, batch in one commit.
5. **#5, #6, #10** — next pass.

Cross-refs: [[DECISIONS]], [[Features#14. Testing-Phase Remote Access + User Guide (branch: desktop)]], [[PROJECT_CONTEXT#Testing-Phase Access (branch: desktop)]].
