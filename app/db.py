import sqlite3
import os
from contextlib import contextmanager

DB_PATH = os.environ.get("DB_PATH", "/app/data/invoices.db")

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    id          TEXT PRIMARY KEY,
    filename    TEXT NOT NULL,
    status      TEXT NOT NULL DEFAULT 'pending',
    invoice_id  TEXT DEFAULT '',
    receiver    TEXT DEFAULT '',
    amount      TEXT DEFAULT '',
    currency    TEXT DEFAULT '',
    due_date    TEXT DEFAULT '',
    iban        TEXT DEFAULT '',
    bic         TEXT DEFAULT '',
    bankgiro    TEXT DEFAULT '',
    plusgiro    TEXT DEFAULT '',
    reference   TEXT DEFAULT '',
    needs_review    TEXT DEFAULT '',
    review_reasons  TEXT DEFAULT '',
    ocr_method  TEXT DEFAULT '',
    error_msg   TEXT DEFAULT '',
    created_at  TEXT DEFAULT (datetime('now')),
    updated_at  TEXT DEFAULT (datetime('now'))
);
"""


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


def upsert_job(job_id: str, **kwargs):
    """Insert job if new, otherwise update only the supplied columns."""
    with get_db() as conn:
        exists = conn.execute("SELECT 1 FROM jobs WHERE id = ?", (job_id,)).fetchone()
        if not exists:
            kwargs["id"] = job_id
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


def get_all_jobs():
    with get_db() as conn:
        return [dict(r) for r in conn.execute(
            "SELECT * FROM jobs ORDER BY created_at DESC"
        ).fetchall()]


def get_job(job_id: str):
    with get_db() as conn:
        r = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
        return dict(r) if r else None
