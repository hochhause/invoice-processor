import sqlite3
import os
from contextlib import contextmanager

DB_PATH = os.environ.get("DB_PATH", "/app/data/invoices.db")

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    id           TEXT PRIMARY KEY,
    filename     TEXT NOT NULL,
    status       TEXT NOT NULL DEFAULT 'LLM-Pending',
    receiver     TEXT DEFAULT '',
    iban         TEXT DEFAULT '',
    bic          TEXT DEFAULT '',
    amount       TEXT DEFAULT '',
    currency     TEXT DEFAULT '',
    due_date     TEXT DEFAULT '',
    reference    TEXT DEFAULT '',
    invoice_id   TEXT DEFAULT '',
    bank_target  TEXT DEFAULT '',
    created_at   TEXT DEFAULT (datetime('now')),
    updated_at   TEXT DEFAULT (datetime('now'))
);
"""

STATUS_ENUM = {'QR-processed', 'LLM-Pending', 'LLM-Done', 'needs_review', 'archived', 'error'}


@contextmanager
def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    with get_db() as conn:
        conn.executescript(SCHEMA)
        # Migration: drop old tables if they exist
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
        if 'vendors' in tables:
            conn.execute("DROP TABLE vendors")
            print("[db] Dropped legacy vendors table", flush=True)
        if 'whitelist' in tables:
            conn.execute("DROP TABLE whitelist")
            print("[db] Dropped legacy whitelist table", flush=True)

        # Wipe existing jobs (testing data)
        conn.execute("DELETE FROM jobs")
        print("[db] Cleared all test job data", flush=True)


def derive_bank_target(currency: str) -> str:
    """Route currency to bank: BKB (CHF/SEK/EUR), Raiffeisen (USD/CAD/GBP), MANUAL (other)."""
    c = (currency or "").upper()
    if c in ("CHF", "SEK", "EUR"):
        return "BKB"
    if c in ("USD", "CAD", "GBP"):
        return "RAIFFEISEN"
    return "MANUAL"


def upsert_job(job_id: str, **kwargs):
    """Insert job if new, otherwise update only supplied columns."""
    # Validate status if provided
    if 'status' in kwargs and kwargs['status'] not in STATUS_ENUM:
        raise ValueError(f"Invalid status: {kwargs['status']}. Must be one of {STATUS_ENUM}")

    with get_db() as conn:
        exists = conn.execute("SELECT 1 FROM jobs WHERE id = ?", (job_id,)).fetchone()
        if not exists:
            kwargs['id'] = job_id
            cols = ", ".join(kwargs.keys())
            placeholders = ", ".join("?" for _ in kwargs)
            conn.execute(
                f"INSERT INTO jobs ({cols}) VALUES ({placeholders})",
                list(kwargs.values()),
            )
        else:
            if not kwargs:
                return
            set_clause = ", ".join(f"{k} = ?" for k in kwargs)
            conn.execute(
                f"UPDATE jobs SET {set_clause}, updated_at = datetime('now') WHERE id = ?",
                [*kwargs.values(), job_id],
            )


def get_jobs(include_archived: bool = False) -> list:
    """Fetch all non-archived jobs by default. Archived hidden from main view."""
    with get_db() as conn:
        if include_archived:
            rows = conn.execute("SELECT * FROM jobs ORDER BY created_at DESC").fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM jobs WHERE status != 'archived' ORDER BY created_at DESC"
            ).fetchall()
        return [dict(r) for r in rows]


def get_job(job_id: str) -> dict | None:
    """Fetch single job by ID."""
    with get_db() as conn:
        r = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
        return dict(r) if r else None


def get_jobs_by_status(status: str) -> list:
    """Fetch all jobs with given status."""
    if status not in STATUS_ENUM:
        raise ValueError(f"Invalid status: {status}")
    with get_db() as conn:
        rows = conn.execute(
            "SELECT * FROM jobs WHERE status = ? ORDER BY created_at DESC",
            (status,)
        ).fetchall()
        return [dict(r) for r in rows]


def get_jobs_by_bank(bank_target: str) -> list:
    """Fetch all non-archived jobs assigned to a bank."""
    with get_db() as conn:
        rows = conn.execute(
            "SELECT * FROM jobs WHERE bank_target = ? AND status != 'archived' ORDER BY created_at DESC",
            (bank_target,)
        ).fetchall()
        return [dict(r) for r in rows]


def set_bank_target(job_id: str, bank_target: str):
    """Override bank_target for a job."""
    if bank_target not in ('BKB', 'RAIFFEISEN', 'MANUAL'):
        raise ValueError(f"Invalid bank_target: {bank_target}")
    with get_db() as conn:
        conn.execute(
            "UPDATE jobs SET bank_target = ?, updated_at = datetime('now') WHERE id = ?",
            (bank_target, job_id)
        )


def archive_jobs(job_ids: list):
    """Mark jobs as archived (hidden from main view)."""
    if not job_ids:
        return
    placeholders = ", ".join("?" * len(job_ids))
    with get_db() as conn:
        conn.execute(
            f"UPDATE jobs SET status = 'archived', updated_at = datetime('now') WHERE id IN ({placeholders})",
            job_ids
        )


def delete_job(job_id: str):
    """Hard delete a single job."""
    with get_db() as conn:
        conn.execute("DELETE FROM jobs WHERE id = ?", (job_id,))


def clear_non_archived():
    """Wipe all non-archived jobs (used by DELETE /api/clear-all)."""
    with get_db() as conn:
        conn.execute("DELETE FROM jobs WHERE status != 'archived'")
