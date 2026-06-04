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

CREATE TABLE IF NOT EXISTS whitelist (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    type        TEXT NOT NULL,
    value       TEXT NOT NULL UNIQUE,
    source      TEXT DEFAULT '',
    created_at  TEXT DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_whitelist_type_value ON whitelist(type, value);
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


def is_whitelisted(type: str, value: str) -> bool:
    """Check if a receiver/iban is in the whitelist."""
    if not value:
        return False
    with get_db() as conn:
        return conn.execute(
            "SELECT 1 FROM whitelist WHERE type = ? AND value = ?",
            (type, value)
        ).fetchone() is not None


def add_to_whitelist(type: str, value: str, source: str = ""):
    """Add receiver/iban to whitelist if not already present."""
    if not value:
        return
    with get_db() as conn:
        try:
            conn.execute(
                "INSERT INTO whitelist (type, value, source) VALUES (?, ?, ?)",
                (type, value, source)
            )
        except Exception:
            pass  # Already exists


def get_whitelist(type: str = None) -> list:
    """Get all whitelisted items, optionally filtered by type."""
    with get_db() as conn:
        if type:
            rows = conn.execute(
                "SELECT * FROM whitelist WHERE type = ? ORDER BY created_at",
                (type,)
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM whitelist ORDER BY type, created_at"
            ).fetchall()
        return [dict(r) for r in rows]


def clear_whitelist(type: str = None):
    """Clear whitelist items (all or by type)."""
    with get_db() as conn:
        if type:
            conn.execute("DELETE FROM whitelist WHERE type = ?", (type,))
        else:
            conn.execute("DELETE FROM whitelist")
