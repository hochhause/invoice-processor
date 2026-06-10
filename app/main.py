"""
main.py — FastAPI web app for LLM-first invoice processing.

Routes (final state):
  GET    /                     → dashboard
  GET    /export               → export screen
  POST   /api/upload           → accept PDF(s) + sync QR scan
  GET    /api/jobs             → JSON list of jobs (archived excluded by default)
  POST   /api/review/{id}      → save edited fields
  POST   /api/run-llm-batch    → trigger LLM on LLM-Pending (+ QR edge) jobs
  POST   /api/assign-bank/{id} → override bank_target
  GET    /api/pdf/{id}         → serve original PDF
  POST   /download/confirm     → generate + zip both pain.001 files, archive sorted jobs
  GET    /download/csv         → export non-archived jobs as CSV
  DELETE /api/jobs/{id}        → hard-delete a single job + its file
  DELETE /api/clear-all        → wipe all non-archived jobs + their files
"""
import csv
import io
import logging
import os
import re
import uuid
import zipfile
from pathlib import Path
from contextlib import asynccontextmanager

# Suppress noisy poll endpoints from uvicorn access log
class _SuppressPolling(logging.Filter):
    _SKIP = {'/api/jobs'}
    def filter(self, record):
        msg = record.getMessage()
        return not any(s in msg for s in self._SKIP)

logging.getLogger('uvicorn.access').addFilter(_SuppressPolling())

from fastapi import BackgroundTasks, Depends, FastAPI, File, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

import config
import cost
import db
import paths
import pipeline
import settings_store
import vendors as vendors_mod
import xml_export
from auth import require_auth

UPLOAD_DIR = paths.upload_dir()
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)

# Columns that may be written back to a job row from pipeline output.
PERSIST_KEYS = {
    "receiver", "iban", "bic", "amount", "currency",
    "reference", "invoice_id", "bank_target", "status",
    "iban_source", "iban_mismatch_db", "match_type",
    "input_tokens", "output_tokens", "llm_model",
    "cdtr_street", "cdtr_building_no", "cdtr_postcode", "cdtr_town", "cdtr_country",
}
# Editable fields accepted by the review form.
REVIEW_FIELDS = ["invoice_id", "receiver", "amount", "currency",
                 "iban", "bic", "reference",
                 "cdtr_street", "cdtr_building_no", "cdtr_postcode", "cdtr_town", "cdtr_country"]
MANDATORY = ["receiver", "iban", "amount", "currency"]


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.init_db()
    # Run startup tests in DEV_MODE
    if os.environ.get("DEV_MODE", "").lower() in ("true", "1", "yes"):
        try:
            import tests
            tests.run_startup_tests()
        except Exception as e:
            print(f"[STARTUP] Tests failed: {e}", flush=True)
            raise
    yield


app = FastAPI(lifespan=lifespan, dependencies=[Depends(require_auth)])
# Absolute paths so the app works when CWD != app/ (PyInstaller bundle, launcher)
_RES = paths.resource_dir()
app.mount("/static", StaticFiles(directory=str(_RES / "static")), name="static")
templates = Jinja2Templates(directory=str(_RES / "templates"))


# ── Helpers ─────────────────────────────────────────────────────────────────

def _accounts() -> dict:
    return config.get_accounts()


def _jobs_needing_llm() -> list:
    """LLM-Pending jobs + QR jobs that still need LLM (non-CHF need a BIC)."""
    pending = db.get_jobs_by_status("LLM-Pending")
    qr_edge = [j for j in db.get_jobs_by_status("QR-processed")
               if (j.get("currency") or "").upper() != "CHF"]
    return pending + qr_edge


def _run_llm_batch(jobs: list):
    """Background worker: run LLM extraction over each job, merge, persist."""
    for job in jobs:
        pdf = next(UPLOAD_DIR.glob(f"{job['id']}_*"), None)
        if not pdf:
            db.upsert_job(job["id"], status="error")
            continue
        try:
            fields = pipeline.run_llm(str(pdf), dict(job))
            fields = {k: v for k, v in fields.items() if k in PERSIST_KEYS}
            db.upsert_job(job["id"], **fields)
        except Exception as e:
            print(f"[llm-batch] {job['id']} failed: {e}", flush=True)
            db.upsert_job(job["id"], status="error")


def _export_blockers() -> list:
    """Non-archived jobs that block export.

    Checks (in priority order, first match wins):
    1. Bad status or missing mandatory pain.001 fields (pre-existing rule).
    2. Routed job (BKB/RAIFFEISEN) whose (bank, ccy) resolves to no configured
       debtor account — would raise ValueError in build_pain001.
    3. Cross-border job (RAIFFEISEN) missing creditor BIC or country — required
       for SWIFT payments; bank rejects the file without them.

    Each entry carries ``blocker_type`` for richer UI messages (backwards-compat
    — existing callers that ignore unknown fields are unaffected).
    """
    blocking_status = ("needs_review", "error", "LLM-Pending")
    blockers = []
    accounts = config.get_accounts()

    for j in db.get_jobs(include_archived=False):
        jid = j["id"]
        fname = j.get("filename", "")
        status = j.get("status", "")

        # Rule 1 — existing hard-block (do not regress)
        missing = [f for f in MANDATORY if not (j.get(f) or "").strip()]
        if missing or status in blocking_status:
            blockers.append({
                "id": jid, "filename": fname, "status": status,
                "missing": missing, "blocker_type": "incomplete",
            })
            continue

        bank = (j.get("bank_target") or "").upper()
        ccy = (j.get("currency") or "").upper()

        # Rule 2 — debtor account unresolvable for this (bank, ccy)
        if bank in accounts["banks"] and ccy:
            acct = config.resolve_account(bank, ccy, accounts)
            if not acct or not acct.get("iban") or not acct.get("bic"):
                blockers.append({
                    "id": jid, "filename": fname, "status": status,
                    "missing": ["debtor_account"],
                    "blocker_type": "unresolvable_account",
                })
                continue

        # Rule 3 — SWIFT payment missing creditor BIC or valid ISO-2 country
        _, add_swift = xml_export._get_service_level(ccy, bank)
        if add_swift:
            xb_missing = [
                f for f in ("bic",)
                if not (j.get(f) or "").strip()
            ]
            country = (j.get("cdtr_country") or "").strip()
            if not re.match(r'^[A-Z]{2}$', country):
                xb_missing.append("cdtr_country")
            if xb_missing:
                blockers.append({
                    "id": jid, "filename": fname, "status": status,
                    "missing": xb_missing,
                    "blocker_type": "cross_border_incomplete",
                })

    return blockers


def _zip_response(files: list) -> StreamingResponse:
    """Bundle (filename, xml_string) pairs into a downloadable zip."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, content in files:
            zf.writestr(name, content)
    buf.seek(0)
    return StreamingResponse(
        iter([buf.getvalue()]),
        media_type="application/zip",
        headers={"Content-Disposition": "attachment; filename=pain001_export.zip"},
    )


# ── Pages ───────────────────────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    jobs = db.get_jobs()
    return templates.TemplateResponse("index.html", {"request": request, "jobs": jobs})


@app.get("/export", response_class=HTMLResponse)
async def export_screen(request: Request):
    jobs = db.get_jobs()
    return templates.TemplateResponse("export.html", {"request": request, "jobs": jobs})


# ── Upload + QR ───────────────────────────────────────────────────────────────

@app.post("/api/upload")
async def upload(files: list[UploadFile] = File(...)):
    """Accept PDF(s), run QR scan synchronously, persist result immediately."""
    created = []
    for f in files:
        if not f.filename or not f.filename.lower().endswith(".pdf"):
            continue
        job_id = str(uuid.uuid4())
        safe_name = os.path.basename(f.filename).replace("/", "_").replace("\\", "_")
        dest = UPLOAD_DIR / f"{job_id}_{safe_name}"
        try:
            dest.write_bytes(await f.read())
        except Exception as e:
            print(f"[upload] write failed for {safe_name}: {e}", flush=True)
            continue
        fields = pipeline.run_qr(str(dest))
        fields = {k: v for k, v in fields.items() if k in PERSIST_KEYS}
        db.upsert_job(job_id, filename=safe_name, **fields)
        created.append(job_id)
    return JSONResponse({"queued": created})


# ── Jobs ────────────────────────────────────────────────────────────────────

@app.get("/api/jobs")
async def api_jobs(include_archived: bool = False):
    return db.get_jobs(include_archived=include_archived)


@app.post("/api/review/{job_id}")
async def save_review(job_id: str, request: Request):
    data = await request.form()
    fields = {k: data[k] for k in REVIEW_FIELDS if k in data}
    if "currency" in fields:
        fields["bank_target"] = db.derive_bank_target(fields["currency"])

    job = db.get_job(job_id) or {}
    merged = {**job, **fields}
    complete = all(merged.get(m) for m in MANDATORY)
    if complete and merged.get("status") == "needs_review":
        fields["status"] = "LLM-Done"
    elif not complete:
        fields["status"] = "needs_review"
    # else: leave existing status untouched (e.g. QR-processed stays as-is)

    # Only reset mismatch audit when operator explicitly changes the IBAN value
    if "iban" in fields and (fields["iban"] or "").strip() != (job.get("iban") or "").strip():
        fields["iban_source"] = "manual"
        fields["iban_mismatch_db"] = ""

    db.upsert_job(job_id, **fields)
    return JSONResponse({"ok": True})


@app.post("/api/run-llm-batch")
async def run_llm_batch(background_tasks: BackgroundTasks):
    jobs = _jobs_needing_llm()
    background_tasks.add_task(_run_llm_batch, jobs)
    return JSONResponse({"queued": len(jobs)})


@app.post("/api/assign-bank/{job_id}")
async def assign_bank(job_id: str, request: Request):
    try:
        data = await request.json()
    except Exception:
        return JSONResponse({"error": "invalid JSON"}, status_code=400)
    bank = (data.get("bank_target") or "").upper()
    try:
        db.set_bank_target(job_id, bank)
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    return JSONResponse({"ok": True})


@app.get("/api/pdf/{job_id}")
async def serve_pdf(job_id: str):
    pdf = next(UPLOAD_DIR.glob(f"{job_id}_*"), None)
    if not pdf:
        return JSONResponse({"error": "not found"}, status_code=404)
    return FileResponse(str(pdf), media_type="application/pdf")


@app.delete("/api/jobs/{job_id}")
async def delete_job(job_id: str):
    for f in UPLOAD_DIR.glob(f"{job_id}_*"):
        f.unlink(missing_ok=True)
    db.delete_job(job_id)
    return JSONResponse({"ok": True})


@app.delete("/api/clear-all")
async def clear_all_jobs():
    """Wipe non-archived jobs and their files. Archived rows + files are kept."""
    for job in db.get_jobs(include_archived=False):
        for f in UPLOAD_DIR.glob(f"{job['id']}_*"):
            f.unlink(missing_ok=True)
    db.clear_non_archived()
    return JSONResponse({"ok": True})


# ── Vendors ──────────────────────────────────────────────────────────────────

@app.get("/api/analytics")
async def analytics():
    with db.get_db() as conn:
        total = conn.execute(
            "SELECT COUNT(*) FROM jobs WHERE status NOT IN ('LLM-Pending', 'error')"
        ).fetchone()[0]
        qr_count = conn.execute(
            "SELECT COUNT(*) FROM jobs WHERE status = 'QR-processed'"
            " OR (status = 'archived' AND (match_type IS NULL OR match_type = ''))"
        ).fetchone()[0]
        text_full = conn.execute(
            "SELECT COUNT(*) FROM jobs WHERE match_type = 'text_full'"
        ).fetchone()[0]
        hybrid = conn.execute(
            "SELECT COUNT(*) FROM jobs WHERE match_type = 'hybrid'"
        ).fetchone()[0]
        image_only = conn.execute(
            "SELECT COUNT(*) FROM jobs WHERE match_type = 'image_only'"
        ).fetchone()[0]
        incomplete = conn.execute(
            "SELECT COUNT(*) FROM jobs WHERE status = 'needs_review'"
        ).fetchone()[0]
        # Token usage per model across ALL jobs (archived included — cost was spent).
        token_rows = conn.execute(
            "SELECT COALESCE(llm_model, '') AS model, "
            "SUM(input_tokens) AS in_tok, SUM(output_tokens) AS out_tok "
            "FROM jobs WHERE input_tokens > 0 OR output_tokens > 0 "
            "GROUP BY llm_model"
        ).fetchall()

    def pct(n):
        return round(n / total * 100, 1) if total else 0.0

    by_model, total_cost, total_in, total_out = [], 0.0, 0, 0
    for r in token_rows:
        in_tok, out_tok = r["in_tok"] or 0, r["out_tok"] or 0
        usd = cost.estimate_cost(r["model"], in_tok, out_tok)
        total_cost += usd
        total_in += in_tok
        total_out += out_tok
        by_model.append({
            "model": r["model"] or "unknown",
            "input_tokens": in_tok,
            "output_tokens": out_tok,
            "usd": round(usd, 4),
        })

    return JSONResponse({
        "total_processed": total,
        "qr_matches":  {"count": qr_count,   "pct": pct(qr_count)},
        "text_full":   {"count": text_full,   "pct": pct(text_full)},
        "hybrid":      {"count": hybrid,      "pct": pct(hybrid)},
        "image_only":  {"count": image_only,  "pct": pct(image_only)},
        "incomplete":  {"count": incomplete,  "pct": pct(incomplete)},
        "cost": {
            "total_usd": round(total_cost, 4),
            "input_tokens": total_in,
            "output_tokens": total_out,
            "by_model": sorted(by_model, key=lambda m: m["usd"], reverse=True),
        },
    })


@app.get("/api/vendors")
async def list_vendors_route():
    return vendors_mod.list_vendors()


@app.post("/api/vendors")
async def create_vendor(request: Request):
    try:
        data = await request.json()
    except Exception:
        return JSONResponse({"error": "invalid JSON"}, status_code=400)
    if not data.get("receiver_name") or not data.get("iban"):
        return JSONResponse({"error": "receiver_name and iban required"}, status_code=400)
    vendors_mod.upsert_vendor(data["receiver_name"], data["iban"], data.get("bic", ""))
    return JSONResponse({"ok": True})


@app.put("/api/vendors/{vendor_id}")
async def update_vendor(vendor_id: str, request: Request):
    try:
        data = await request.json()
    except Exception:
        return JSONResponse({"error": "invalid JSON"}, status_code=400)
    if not data.get("receiver_name") or not data.get("iban"):
        return JSONResponse({"error": "receiver_name and iban required"}, status_code=400)
    vendors_mod.update_vendor(vendor_id, data["receiver_name"], data["iban"], data.get("bic", ""))
    return JSONResponse({"ok": True})


@app.delete("/api/vendors/{vendor_id}")
async def delete_vendor_route(vendor_id: str):
    vendors_mod.delete_vendor(vendor_id)
    return JSONResponse({"ok": True})


# ── Settings (desktop mode) ───────────────────────────────────────────────────

_BANK_RE = re.compile(r"^[A-Z]+$")
_CCY_RE = re.compile(r"^[A-Z]{3}$")
_BIC_RE = re.compile(r"^[A-Z]{6}[A-Z0-9]{2}([A-Z0-9]{3})?$")


def _bank_env_keys(banks: list) -> set:
    """Env keys owned by a bank-config list (parsed shape of /api/settings).

    Includes the bank-level ``{BANK}_BIC`` fallback: the popup saves explicit
    per-ccy BICs (GET already resolves the fallback into each account), so the
    bank-level key is always superseded on save and cleaned up here."""
    keys = set()
    for b in banks:
        name = b["name"]
        keys.add(f"{name}_CURRENCIES")
        keys.add(f"{name}_DEFAULT_CCY")
        keys.add(f"{name}_BIC")
        for acct in b["accounts"]:
            keys.add(f"{name}_{acct['ccy']}_IBAN")
            keys.add(f"{name}_{acct['ccy']}_BIC")
    return keys


def _current_settings() -> dict:
    """Full editable config, read fresh from the environment (no cache)."""
    cfg = config.load_accounts()
    banks = []
    for bank in cfg["banks"]:
        accounts = [
            {"ccy": ccy, "iban": acct.get("iban", ""), "bic": acct.get("bic", "")}
            for ccy, acct in sorted(cfg["accounts"].get(bank, {}).items())
        ]
        banks.append({
            "name": bank,
            "currencies": sorted(cfg["currencies"].get(bank, set())),
            "default_ccy": cfg["defaults"].get(bank, ""),
            "accounts": accounts,
        })
    return {
        "desktop": paths.is_desktop(),
        "api_key_set": bool(os.environ.get("ANTHROPIC_API_KEY", "").strip()),
        "debtor_name": cfg["debtor_name"],
        "llm_model": os.environ.get("LLM_MODEL", ""),
        "banks": banks,
    }


def _parse_settings_payload(data: dict) -> tuple[dict | None, str | None]:
    """Validate + normalize a POST /api/settings payload. → (parsed, error)."""
    debtor_name = (data.get("debtor_name") or "").strip()
    llm_model = (data.get("llm_model") or "").strip()
    api_key = (data.get("api_key") or "").strip()
    if api_key and any(c.isspace() for c in api_key):
        return None, "API key must be a single token"

    banks, seen = [], set()
    for b in data.get("banks") or []:
        name = (b.get("name") or "").strip().upper()
        if not _BANK_RE.match(name):
            return None, f"Bank name must be letters only: {name or '(empty)'}"
        if name in seen:
            return None, f"Duplicate bank: {name}"
        seen.add(name)

        currencies = [c.strip().upper() for c in (b.get("currencies") or []) if c.strip()]
        for c in currencies:
            if not _CCY_RE.match(c):
                return None, f"{name}: invalid currency code {c!r}"
        if not currencies:
            return None, f"{name}: at least one currency required"

        default_ccy = (b.get("default_ccy") or "").strip().upper()
        if default_ccy and default_ccy not in currencies:
            return None, f"{name}: default currency {default_ccy} not in its currency list"
        if not default_ccy:
            default_ccy = currencies[0]

        accounts, seen_ccy = [], set()
        for acct in b.get("accounts") or []:
            ccy = (acct.get("ccy") or "").strip().upper()
            iban = re.sub(r"\s", "", acct.get("iban") or "").upper()
            bic = (acct.get("bic") or "").strip().upper()
            if not _CCY_RE.match(ccy):
                return None, f"{name}: account has invalid currency {ccy!r}"
            if ccy in seen_ccy:
                return None, f"{name}: duplicate {ccy} account"
            seen_ccy.add(ccy)
            if iban and not config._validate_iban(iban):
                return None, f"{name} {ccy}: IBAN fails checksum — please re-check"
            if bic and not _BIC_RE.match(bic):
                return None, f"{name} {ccy}: BIC format invalid (8 or 11 chars)"
            accounts.append({"ccy": ccy, "iban": iban, "bic": bic})

        banks.append({"name": name, "currencies": currencies,
                      "default_ccy": default_ccy, "accounts": accounts})

    return {"debtor_name": debtor_name, "llm_model": llm_model,
            "api_key": api_key, "banks": banks}, None


@app.get("/api/settings/status")
async def settings_status():
    """First-run probe for the desktop app: is the Anthropic key configured?

    ``desktop`` tells the frontend whether settings can be saved via the API
    (container deployments keep configuring everything through env/.env)."""
    return JSONResponse({
        "desktop": paths.is_desktop(),
        "api_key_set": bool(os.environ.get("ANTHROPIC_API_KEY", "").strip()),
    })


@app.get("/api/settings")
async def get_settings():
    """Everything the settings popup edits: payee, model, banks/accounts."""
    return JSONResponse(_current_settings())


@app.post("/api/settings")
async def save_settings(request: Request):
    """Persist the full config to <app-data>/settings.env (desktop only).

    Replaces all bank-derived env keys: keys for banks/accounts that no longer
    exist in the payload are removed from file + environment. Applies live —
    config cache is cleared, llm reads env per call."""
    if not paths.is_desktop():
        return JSONResponse(
            {"error": "settings are managed via environment in server mode"},
            status_code=403,
        )
    try:
        data = await request.json()
    except Exception:
        return JSONResponse({"error": "invalid JSON"}, status_code=400)

    parsed, err = _parse_settings_payload(data)
    if err:
        return JSONResponse({"error": err}, status_code=400)

    values = {"DEBTOR_NAME": parsed["debtor_name"]}
    if parsed["llm_model"]:
        values["LLM_MODEL"] = parsed["llm_model"]
        values["LLM_MODEL_TEXT"] = parsed["llm_model"]
    if parsed["api_key"]:
        values["ANTHROPIC_API_KEY"] = parsed["api_key"]
    for b in parsed["banks"]:
        values[f"{b['name']}_CURRENCIES"] = ",".join(b["currencies"])
        values[f"{b['name']}_DEFAULT_CCY"] = b["default_ccy"]
        for acct in b["accounts"]:
            values[f"{b['name']}_{acct['ccy']}_IBAN"] = acct["iban"]
            values[f"{b['name']}_{acct['ccy']}_BIC"] = acct["bic"]

    # Bank keys that existed before but are gone now → delete from file + env
    old_cfg = config.load_accounts()
    old_banks = [{"name": bank,
                  "accounts": [{"ccy": c} for c in old_cfg["accounts"].get(bank, {})]}
                 for bank in old_cfg["banks"]]
    stale = _bank_env_keys(old_banks) - set(values)

    settings_store.set_many(values, remove=sorted(stale))
    config._clear_cache()
    return JSONResponse({"ok": True, "settings": _current_settings()})


# ── Export / Download ─────────────────────────────────────────────────────────

@app.get("/api/export-readiness")
async def export_readiness():
    """Report whether export can proceed. Frontend uses this to gate the
    Export/Download action and to render the blocker popup."""
    blockers = _export_blockers()
    return JSONResponse({"ready": not blockers, "blockers": blockers})


@app.get("/api/accounts-summary")
async def accounts_summary():
    """Per-bank account-resolution map for the export UI.

    Lets the export board show *which debtor account* each currency block
    debits (e.g. SEK → BKB-CHF account) without duplicating the resolution
    rule on the frontend. Config-driven — same `config.resolve_account` that
    `derive_bank_target`/`build_pain001` use, so there is no drift.

    Shape: ``{BANK: {default_ccy, resolve: {ccy: account_ccy}}}``. A currency
    dragged into a bank that is not in its CURRENCIES list is not listed here;
    the frontend falls back to ``default_ccy`` for it (mirrors the resolver)."""
    cfg = config.get_accounts()
    out = {}
    for bank in cfg["banks"]:
        resolve = {}
        for ccy in sorted(cfg["currencies"].get(bank, set())):
            acct = config.resolve_account(bank, ccy, cfg)
            if acct:
                resolve[ccy] = acct.get("ccy", ccy)
        out[bank] = {
            "default_ccy": cfg["defaults"].get(bank, ""),
            "resolve": resolve,
        }
    return JSONResponse(out)


@app.post("/download/confirm")
async def download_confirm():
    """Generate pain.001 for BKB + Raiffeisen, zip them, archive sorted jobs.

    Refuses (409) if any non-archived invoice still needs review or is missing
    mandatory pain.001 fields — caller shows the popup and the operator fixes
    them before retrying."""
    blockers = _export_blockers()
    if blockers:
        return JSONResponse(
            {"error": "Some invoices are not export-ready. Resolve them before exporting.",
             "blockers": blockers},
            status_code=409,
        )

    accounts = _accounts()
    banks = accounts["banks"]

    jobs_by_bank = {bank: db.get_jobs_by_bank(bank) for bank in banks}
    all_jobs = [j for jobs in jobs_by_bank.values() for j in jobs]

    if not all_jobs:
        return JSONResponse({"error": "no sorted jobs to export"}, status_code=400)

    files = []
    try:
        for bank, jobs in jobs_by_bank.items():
            if jobs:
                files.append((f"pain001_{bank}.xml",
                              xml_export.build_pain001(jobs, accounts, bank=bank)))
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)

    db.archive_jobs([j["id"] for j in all_jobs])
    return _zip_response(files)


@app.get("/download/csv")
async def download_csv():
    jobs = db.get_jobs(include_archived=False)
    fields = ["filename", "receiver", "iban", "bic", "amount", "currency",
              "reference", "invoice_id", "bank_target", "status"]
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=fields, extrasaction="ignore")
    w.writeheader()
    for j in jobs:
        w.writerow(j)
    buf.seek(0)
    return StreamingResponse(
        iter([buf.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=invoices.csv"},
    )
