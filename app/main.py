"""
main.py — FastAPI web app for invoice processing.

Routes:
  GET  /              → dashboard
  POST /upload        → accept PDF(s), queue jobs
  POST /process/{id}  → start processing a queued job
  POST /process-all   → process all pending jobs
  GET  /api/jobs      → JSON list of all jobs (for HTMX polling)
  GET  /api/llm-available → check if Tier 1 LLM fallback is configured
  POST /api/retry-ai/{id} → extract missing fields with Haiku (Tier 1)
  GET  /download/csv  → download all processed rows as CSV
  DELETE /jobs/{id}   → remove a job record + its file
"""
import csv
import io
import json
import os
import uuid
from pathlib import Path
from contextlib import asynccontextmanager

from fastapi import BackgroundTasks, FastAPI, File, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, StreamingResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

import db
import pipeline
import xml_export
from config import llm_available
from extract import llm_extract_fields, evaluate_field_statuses

UPLOAD_DIR = Path(os.environ.get("UPLOAD_DIR", "/app/data/uploads"))
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)


def _probe_llm():
    """Runs every startup — prints exact failure reason to log. Never raises."""
    print("[LLM-PROBE] ── starting ──────────────────────────────", flush=True)

    flag = os.getenv("ENABLE_LLM_FALLBACK", "")
    print(f"[LLM-PROBE] ENABLE_LLM_FALLBACK = {repr(flag)}", flush=True)
    if flag.lower() not in ("true", "1", "yes"):
        print("[LLM-PROBE] RESULT: disabled (flag not true) ✗", flush=True)
        return

    key = os.getenv("ANTHROPIC_API_KEY", "")
    print(f"[LLM-PROBE] ANTHROPIC_API_KEY present={bool(key.strip())} prefix={key.strip()[:12]!r}", flush=True)
    if not key.strip():
        print("[LLM-PROBE] RESULT: disabled (no key) ✗", flush=True)
        return
    if not key.strip().startswith("sk-ant-"):
        print("[LLM-PROBE] RESULT: disabled (bad key prefix) ✗", flush=True)
        return

    try:
        from anthropic import Anthropic
        print("[LLM-PROBE] anthropic package: imported OK", flush=True)
    except ImportError as e:
        print(f"[LLM-PROBE] RESULT: disabled (ImportError: {e}) ✗", flush=True)
        return

    try:
        client = Anthropic(api_key=key.strip())
        resp = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=16,
            messages=[{"role": "user", "content": "Reply with: OK"}],
        )
        text = resp.content[0].text.strip()
        print(f"[LLM-PROBE] API response: {repr(text)} stop={resp.stop_reason}", flush=True)
        print("[LLM-PROBE] RESULT: available ✓", flush=True)
    except Exception as e:
        print(f"[LLM-PROBE] RESULT: API call failed — {type(e).__name__}: {e} ✗", flush=True)

    # Prime the lru_cache after probe so /api/retry-ai picks up the result
    llm_available()
    print("[LLM-PROBE] ── done ──────────────────────────────────", flush=True)


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.init_db()
    _probe_llm()
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


# ── Background tasks ──────────────────────────────────────────────────────────

def _process_job(job_id: str, pdf_path: str):
    db.upsert_job(job_id, status="processing")
    try:
        fields = pipeline.run(pdf_path)
        fields.pop("flags", None)  # in-memory only, not a DB column
        matched_vendor_id = fields.pop("_matched_vendor_id", None)
        # Re-evaluate statuses from final extracted values, then serialize
        fields["field_statuses"] = json.dumps(evaluate_field_statuses(fields))
        db.upsert_job(job_id, status="done", **fields)
        # Persist invoice_id into vendor history for future format+range matching
        if matched_vendor_id and fields.get("invoice_id"):
            db.add_invoice_id_to_vendor(matched_vendor_id, fields["invoice_id"])
    except Exception as e:
        db.upsert_job(job_id, status="error", error_msg=str(e))


# ── Routes ────────────────────────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    jobs = db.get_all_jobs()
    return templates.TemplateResponse("index.html", {"request": request, "jobs": jobs})


@app.post("/upload")
async def upload(background_tasks: BackgroundTasks, files: list[UploadFile] = File(...)):
    created = []
    for f in files:
        if not f.filename.lower().endswith(".pdf"):
            continue
        job_id = str(uuid.uuid4())
        dest = UPLOAD_DIR / f"{job_id}_{f.filename}"
        content = await f.read()
        dest.write_bytes(content)
        db.upsert_job(job_id, filename=f.filename, status="pending")
        background_tasks.add_task(_process_job, job_id, str(dest))
        created.append(job_id)
    return JSONResponse({"queued": created})


@app.post("/process/{job_id}")
async def process_one(job_id: str, background_tasks: BackgroundTasks):
    job = db.get_job(job_id)
    if not job:
        return JSONResponse({"error": "not found"}, status_code=404)
    pdf = next(UPLOAD_DIR.glob(f"{job_id}_*"), None)
    if not pdf:
        return JSONResponse({"error": "file missing"}, status_code=404)
    background_tasks.add_task(_process_job, job_id, str(pdf))
    return JSONResponse({"ok": True})


@app.post("/process-all")
async def process_all(background_tasks: BackgroundTasks):
    jobs = [j for j in db.get_all_jobs()
            if j["status"] in ("pending", "error")
            or (j["status"] == "done" and j.get("needs_review") == "YES")]
    for job in jobs:
        pdf = next(UPLOAD_DIR.glob(f"{job['id']}_*"), None)
        if pdf:
            background_tasks.add_task(_process_job, job["id"], str(pdf))
            db.upsert_job(job["id"], status="processing")
    return JSONResponse({"queued": len(jobs)})


@app.get("/api/jobs")
async def api_jobs():
    jobs = db.get_all_jobs()
    for job in jobs:
        fs = job.get("field_statuses", "{}")
        if isinstance(fs, str):
            try:
                job["field_statuses"] = json.loads(fs)
            except (json.JSONDecodeError, TypeError):
                job["field_statuses"] = {}
    return jobs


@app.delete("/jobs/{job_id}")
async def delete_job(job_id: str):
    for f in UPLOAD_DIR.glob(f"{job_id}_*"):
        f.unlink(missing_ok=True)
    # Also delete debug markdown files if DEBUG_MD_DIR set
    pipeline._delete_debug_md(job_id, UPLOAD_DIR)
    with db.get_db() as conn:
        conn.execute("DELETE FROM jobs WHERE id = ?", (job_id,))
    return JSONResponse({"ok": True})


@app.delete("/api/clear-all")
async def clear_all_jobs():
    for f in UPLOAD_DIR.glob("*_*"):
        f.unlink(missing_ok=True)
    # Also delete all debug markdown files if DEBUG_MD_DIR set
    debug_md_dir = os.environ.get("DEBUG_MD_DIR", "")
    if debug_md_dir:
        debug_path = Path(debug_md_dir)
        for f in debug_path.glob("*.md"):
            f.unlink(missing_ok=True)
    with db.get_db() as conn:
        conn.execute("DELETE FROM jobs")
    return JSONResponse({"ok": True})


@app.get("/api/pdf/{job_id}")
async def serve_pdf(job_id: str):
    pdf = next(UPLOAD_DIR.glob(f"{job_id}_*"), None)
    if not pdf:
        return JSONResponse({"error": "not found"}, status_code=404)
    return FileResponse(str(pdf), media_type="application/pdf")


REVIEW_FIELDS = ["invoice_id", "receiver", "amount", "currency", "due_date",
                 "iban", "bic", "reference"]


@app.post("/api/review/{job_id}")
async def save_review(job_id: str, request: Request):
    from extract import validate_job_fields
    data = await request.form()
    fields = {k: data[k] for k in REVIEW_FIELDS if k in data}

    job = db.get_job(job_id)
    merged = {**job, **fields} if job else fields

    validation = validate_job_fields(merged)
    fields["needs_review"] = validation["needs_review"]
    fields["review_reasons"] = validation["review_reasons"]

    # Re-evaluate field statuses from the merged (post-edit) values
    fields["field_statuses"] = json.dumps(evaluate_field_statuses(merged))

    db.upsert_job(job_id, **fields)
    return JSONResponse({"ok": True})




@app.get("/download/csv")
async def download_csv():
    jobs = [j for j in db.get_all_jobs() if j["status"] == "done"]
    fields = ["filename", "invoice_id", "receiver", "iban", "bic",
              "amount", "currency", "due_date", "reference", "needs_review", "review_reasons", "ocr_method"]
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


@app.get("/download/xml")
async def download_xml():
    """Download ISO 20022 pain.001.001.03 XML for mass payment import."""
    jobs = db.get_all_jobs()
    debtor = {
        "name": os.getenv("DEBTOR_NAME", "My Company"),
        "iban": os.getenv("DEBTOR_IBAN", ""),
        "bic": os.getenv("DEBTOR_BIC", ""),
    }
    xml_str = xml_export.build_pain001(jobs, debtor)
    return Response(
        content=xml_str,
        media_type="application/xml",
        headers={"Content-Disposition": "attachment; filename=pain001.xml"},
    )


@app.get("/api/llm-available")
async def api_llm_available():
    """Check if Tier 1 LLM fallback is configured and working."""
    import io, contextlib
    available = llm_available()
    # Expose env var presence for diagnostics (no key value)
    return {
        "available": available,
        "flag_set": os.getenv("ENABLE_LLM_FALLBACK", "") in ("true", "1", "yes"),
        "key_present": bool(os.getenv("ANTHROPIC_API_KEY", "").strip()),
        "key_prefix_ok": os.getenv("ANTHROPIC_API_KEY", "").strip().startswith("sk-ant-"),
    }


@app.get("/api/vendors")
async def api_get_vendors():
    return db.get_vendors()


@app.post("/api/vendors")
async def api_create_vendor(request: Request):
    data = await request.json()
    name = (data.get("name") or "").strip()
    iban = (data.get("iban") or "").strip()
    if not name and not iban:
        return JSONResponse({"error": "name or iban required"}, status_code=400)
    vid = db.upsert_vendor(None, name, iban)
    return JSONResponse({"id": vid})


@app.put("/api/vendors/{vendor_id}")
async def api_update_vendor(vendor_id: int, request: Request):
    data = await request.json()
    name = (data.get("name") or "").strip()
    iban = (data.get("iban") or "").strip()
    db.upsert_vendor(vendor_id, name, iban)
    return JSONResponse({"ok": True})


@app.delete("/api/vendors/{vendor_id}")
async def api_delete_vendor(vendor_id: int):
    db.delete_vendor(vendor_id)
    return JSONResponse({"ok": True})


# Legacy shims kept for dev-panel flush button
@app.get("/api/whitelist")
async def get_whitelist(type: str = None):
    return db.get_vendors()


@app.delete("/api/whitelist")
async def clear_whitelist(type: str = None):
    db.clear_whitelist(type)
    return JSONResponse({"ok": True})


@app.post("/api/retry-ai/{job_id}")
async def retry_with_ai(job_id: str):
    """Reprocess EMPTY and SUSPICIOUS fields with Tier 1 (Haiku) markdown extraction."""
    if not llm_available():
        return JSONResponse(
            {"error": "LLM fallback not configured"}, status_code=503
        )

    job = db.get_job(job_id)
    if not job:
        return JSONResponse({"error": "Job not found"}, status_code=404)

    # Get markdown from debug dir
    debug_md_dir = os.environ.get("DEBUG_MD_DIR", "")
    if not debug_md_dir:
        return JSONResponse(
            {"error": "Markdown not available (DEBUG_MD_DIR not set)"}, status_code=400
        )

    filename_stem = Path(job['filename']).stem
    md_path = Path(debug_md_dir) / f"{job_id}_{filename_stem}.md"
    if not md_path.exists():
        return JSONResponse(
            {"error": "Markdown file not found"}, status_code=400
        )

    md = md_path.read_text()

    # Parse stored field_statuses
    fs_raw = job.get("field_statuses", "{}")
    try:
        field_statuses = json.loads(fs_raw) if isinstance(fs_raw, str) else fs_raw or {}
    except (json.JSONDecodeError, TypeError):
        field_statuses = {}

    # Send EMPTY and SUSPICIOUS fields to AI (not SUCCESSFUL ones)
    candidate_fields = ["invoice_id", "amount", "currency", "receiver", "due_date", "iban", "bic", "reference"]
    ai_fields = []
    for field in candidate_fields:
        status = field_statuses.get(field, "EMPTY")
        if status in ("EMPTY", "SUSPICIOUS"):
            ai_fields.append(field)

    if not ai_fields:
        return JSONResponse({"status": "all_fields_successful", "updated": {}})

    # Extract all EMPTY/SUSPICIOUS fields in a single Haiku call
    updates = llm_extract_fields(md, ai_fields)

    if updates:
        updates["ocr_method"] = "docling+llm"
        # Re-evaluate statuses from merged job+AI values
        merged = {**job, **updates}
        updates["field_statuses"] = json.dumps(evaluate_field_statuses(merged))
        db.upsert_job(job_id, **updates)

    return JSONResponse({"updated": {k: v for k, v in updates.items() if k not in ("ocr_method", "field_statuses")},
                         "status": "retry_complete",
                         "ai_fields": ai_fields})
