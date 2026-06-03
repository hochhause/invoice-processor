"""
main.py — FastAPI web app for invoice processing.

Routes:
  GET  /              → dashboard
  POST /upload        → accept PDF(s), queue jobs
  POST /process/{id}  → start processing a queued job
  POST /process-all   → process all pending jobs
  GET  /api/jobs      → JSON list of all jobs (for HTMX polling)
  GET  /download/csv  → download all processed rows as CSV
  DELETE /jobs/{id}   → remove a job record + its file
"""
import csv
import io
import os
import uuid
from pathlib import Path
from contextlib import asynccontextmanager

from fastapi import BackgroundTasks, FastAPI, File, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

import db
import pipeline

UPLOAD_DIR = Path(os.environ.get("UPLOAD_DIR", "/app/data/uploads"))
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.init_db()
    yield


app = FastAPI(lifespan=lifespan)
app.mount("/static", StaticFiles(directory="static"), name="static")
templates = Jinja2Templates(directory="templates")


# ── Background tasks ──────────────────────────────────────────────────────────

def _process_job(job_id: str, pdf_path: str, force_llm: bool = False):
    db.upsert_job(job_id, status="processing")
    try:
        fields = pipeline.run(pdf_path, force_llm=force_llm)
        db.upsert_job(job_id, status="done", **fields)
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
    jobs = [j for j in db.get_all_jobs() if j["status"] in ("pending", "error")]
    for job in jobs:
        pdf = next(UPLOAD_DIR.glob(f"{job['id']}_*"), None)
        if pdf:
            background_tasks.add_task(_process_job, job["id"], str(pdf))
            db.upsert_job(job["id"], status="processing")
    return JSONResponse({"queued": len(jobs)})


@app.get("/api/jobs")
async def api_jobs():
    return db.get_all_jobs()


@app.delete("/jobs/{job_id}")
async def delete_job(job_id: str):
    for f in UPLOAD_DIR.glob(f"{job_id}_*"):
        f.unlink(missing_ok=True)
    with db.get_db() as conn:
        conn.execute("DELETE FROM jobs WHERE id = ?", (job_id,))
    return JSONResponse({"ok": True})


@app.get("/api/pdf/{job_id}")
async def serve_pdf(job_id: str):
    pdf = next(UPLOAD_DIR.glob(f"{job_id}_*"), None)
    if not pdf:
        return JSONResponse({"error": "not found"}, status_code=404)
    return FileResponse(str(pdf), media_type="application/pdf")


REVIEW_FIELDS = ["invoice_id", "receiver", "amount", "currency", "due_date",
                 "iban", "bic", "bankgiro", "plusgiro", "reference"]


@app.post("/api/review/{job_id}")
async def save_review(job_id: str, request: Request):
    data = await request.form()
    fields = {k: data[k] for k in REVIEW_FIELDS if k in data}
    fields["needs_review"] = "NO"
    fields["review_reasons"] = ""
    db.upsert_job(job_id, **fields)
    return JSONResponse({"ok": True})


@app.post("/api/queue-llm/{job_id}")
async def queue_llm_one(job_id: str, background_tasks: BackgroundTasks):
    job = db.get_job(job_id)
    if not job:
        return JSONResponse({"error": "not found"}, status_code=404)
    pdf = next(UPLOAD_DIR.glob(f"{job_id}_*"), None)
    if not pdf:
        return JSONResponse({"error": "file missing"}, status_code=404)
    db.upsert_job(job_id, status="pending")
    background_tasks.add_task(_process_job, job_id, str(pdf), True)
    return JSONResponse({"ok": True})


@app.post("/api/queue-llm-all")
async def queue_llm_all(background_tasks: BackgroundTasks):
    jobs = [j for j in db.get_all_jobs()
            if j.get("needs_review") == "YES" and j.get("ocr_method") in ("mdx", "", None)]
    for job in jobs:
        pdf = next(UPLOAD_DIR.glob(f"{job['id']}_*"), None)
        if pdf:
            db.upsert_job(job["id"], status="pending")
            background_tasks.add_task(_process_job, job["id"], str(pdf), True)
    return JSONResponse({"queued": len(jobs)})


@app.get("/download/csv")
async def download_csv():
    jobs = [j for j in db.get_all_jobs() if j["status"] == "done"]
    fields = ["filename", "invoice_id", "receiver", "iban", "bic",
              "bankgiro", "plusgiro", "amount", "currency", "due_date",
              "reference", "needs_review", "review_reasons", "ocr_method"]
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
