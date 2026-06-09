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

from fastapi import BackgroundTasks, FastAPI, File, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

import db
import pipeline
import vendors as vendors_mod
import xml_export

UPLOAD_DIR = Path(os.environ.get("UPLOAD_DIR", "/app/data/uploads"))
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)

# Columns that may be written back to a job row from pipeline output.
PERSIST_KEYS = {
    "receiver", "iban", "bic", "amount", "currency",
    "reference", "invoice_id", "bank_target", "status",
    "iban_source", "iban_mismatch_db", "match_type",
}
# Editable fields accepted by the review form.
REVIEW_FIELDS = ["invoice_id", "receiver", "amount", "currency",
                 "iban", "bic", "reference"]
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


app = FastAPI(lifespan=lifespan)
app.mount("/static", StaticFiles(directory="static"), name="static")
templates = Jinja2Templates(directory="templates")


# ── Helpers ─────────────────────────────────────────────────────────────────

def _debtor() -> dict:
    return {
        "name": os.getenv("DEBTOR_NAME", "My Company"),
        "iban": os.getenv("DEBTOR_IBAN", ""),
        "bic": os.getenv("DEBTOR_BIC", ""),
    }


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
    """Non-archived jobs that block export: bad status or missing mandatory
    pain.001 fields. Returns one entry per blocking job for the UI popup."""
    blocking_status = ("needs_review", "error", "LLM-Pending")
    blockers = []
    for j in db.get_jobs(include_archived=False):
        missing = [f for f in MANDATORY if not (j.get(f) or "").strip()]
        if missing or j.get("status") in blocking_status:
            blockers.append({
                "id": j["id"],
                "filename": j.get("filename", ""),
                "status": j.get("status", ""),
                "missing": missing,
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

    # Operator save = manual IBAN source; clear mismatch flag
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

    def pct(n):
        return round(n / total * 100, 1) if total else 0.0

    return JSONResponse({
        "total_processed": total,
        "qr_matches":  {"count": qr_count,   "pct": pct(qr_count)},
        "text_full":   {"count": text_full,   "pct": pct(text_full)},
        "hybrid":      {"count": hybrid,      "pct": pct(hybrid)},
        "image_only":  {"count": image_only,  "pct": pct(image_only)},
        "incomplete":  {"count": incomplete,  "pct": pct(incomplete)},
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


# ── Export / Download ─────────────────────────────────────────────────────────

@app.get("/api/export-readiness")
async def export_readiness():
    """Report whether export can proceed. Frontend uses this to gate the
    Export/Download action and to render the blocker popup."""
    blockers = _export_blockers()
    return JSONResponse({"ready": not blockers, "blockers": blockers})


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

    debtor = _debtor()
    bkb_jobs = db.get_jobs_by_bank("BKB")
    raiff_jobs = db.get_jobs_by_bank("RAIFFEISEN")

    if not bkb_jobs and not raiff_jobs:
        return JSONResponse({"error": "no sorted jobs to export"}, status_code=400)

    files = []
    try:
        if bkb_jobs:
            files.append(("pain001_BKB.xml",
                          xml_export.build_pain001(bkb_jobs, debtor, bank="BKB")))
        if raiff_jobs:
            files.append(("pain001_Raiffeisen.xml",
                          xml_export.build_pain001(raiff_jobs, debtor, bank="RAIFFEISEN")))
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)

    db.archive_jobs([j["id"] for j in bkb_jobs + raiff_jobs])
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
