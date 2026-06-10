import sqlite3
import os
from contextlib import contextmanager

import paths

DB_PATH = paths.db_path()

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    id               TEXT PRIMARY KEY,
    filename         TEXT NOT NULL,
    status           TEXT NOT NULL DEFAULT 'LLM-Pending',
    receiver         TEXT DEFAULT '',
    iban             TEXT DEFAULT '',
    bic              TEXT DEFAULT '',
    amount           TEXT DEFAULT '',
    currency         TEXT DEFAULT '',
    reference        TEXT DEFAULT '',
    invoice_id       TEXT DEFAULT '',
    bank_target      TEXT DEFAULT '',
    iban_source      TEXT DEFAULT '',
    iban_mismatch_db TEXT DEFAULT '',
    match_type       TEXT DEFAULT '',
    input_tokens     INTEGER DEFAULT 0,
    output_tokens    INTEGER DEFAULT 0,
    llm_model        TEXT DEFAULT '',
    cdtr_street      TEXT DEFAULT '',
    cdtr_building_no TEXT DEFAULT '',
    cdtr_postcode    TEXT DEFAULT '',
    cdtr_town        TEXT DEFAULT '',
    cdtr_country     TEXT DEFAULT '',
    created_at       TEXT DEFAULT (datetime('now')),
    updated_at       TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS vendors (
    id            TEXT PRIMARY KEY DEFAULT (lower(hex(randomblob(8)))),
    receiver_name TEXT NOT NULL,
    iban          TEXT NOT NULL,
    bic           TEXT DEFAULT '',
    created_at    TEXT DEFAULT (datetime('now')),
    updated_at    TEXT DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_vendors_receiver ON vendors(lower(receiver_name));
"""

STATUS_ENUM = {'QR-processed', 'LLM-Pending', 'LLM-Done', 'needs_review', 'archived', 'error'}

_JOB_COLUMNS = {
    'id', 'filename', 'status', 'receiver', 'iban', 'bic', 'amount',
    'currency', 'reference', 'invoice_id', 'bank_target', 'iban_source',
    'iban_mismatch_db', 'match_type', 'input_tokens', 'output_tokens',
    'llm_model', 'cdtr_street', 'cdtr_building_no', 'cdtr_postcode',
    'cdtr_town', 'cdtr_country', 'created_at', 'updated_at',
}


@contextmanager
def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    except:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    with get_db() as conn:
        conn.executescript(SCHEMA)

        # Migration: drop legacy tables
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
        if 'whitelist' in tables:
            conn.execute("DROP TABLE whitelist")
            print("[db] Dropped legacy whitelist table", flush=True)

        # Migration tracking table — records one-time data migrations by ID
        conn.execute("""
            CREATE TABLE IF NOT EXISTS schema_migrations (
                id         TEXT PRIMARY KEY,
                applied_at TEXT DEFAULT (datetime('now'))
            )
        """)

        # Migration: swap iban/iban_mismatch_db for document_mismatch rows.
        # pipeline.run_vendor_check semantics flipped: iban now holds vendor IBAN
        # (authoritative), iban_mismatch_db holds the doc-extracted IBAN (audit).
        # Pre-existing rows have the inverse layout — swap them once.
        _mig = 'swap_iban_mismatch_semantics_v1'
        if not conn.execute("SELECT 1 FROM schema_migrations WHERE id=?", (_mig,)).fetchone():
            conn.execute("""
                UPDATE jobs
                SET iban          = iban_mismatch_db,
                    iban_mismatch_db = iban
                WHERE iban_source = 'document_mismatch'
                  AND iban          != ''
                  AND iban_mismatch_db != ''
            """)
            conn.execute("INSERT INTO schema_migrations (id) VALUES (?)", (_mig,))

        # Migration: add later columns to existing DBs (SQLite has no ADD COLUMN IF NOT EXISTS)
        for col_def in [
            "ALTER TABLE jobs ADD COLUMN iban_source TEXT DEFAULT ''",
            "ALTER TABLE jobs ADD COLUMN iban_mismatch_db TEXT DEFAULT ''",
            "ALTER TABLE jobs ADD COLUMN match_type TEXT DEFAULT ''",
            "ALTER TABLE jobs ADD COLUMN input_tokens INTEGER DEFAULT 0",
            "ALTER TABLE jobs ADD COLUMN output_tokens INTEGER DEFAULT 0",
            "ALTER TABLE jobs ADD COLUMN llm_model TEXT DEFAULT ''",
            "ALTER TABLE jobs ADD COLUMN cdtr_street TEXT DEFAULT ''",
            "ALTER TABLE jobs ADD COLUMN cdtr_building_no TEXT DEFAULT ''",
            "ALTER TABLE jobs ADD COLUMN cdtr_postcode TEXT DEFAULT ''",
            "ALTER TABLE jobs ADD COLUMN cdtr_town TEXT DEFAULT ''",
            "ALTER TABLE jobs ADD COLUMN cdtr_country TEXT DEFAULT ''",
        ]:
            try:
                conn.execute(col_def)
            except sqlite3.OperationalError as e:
                if "duplicate column name" not in str(e):
                    raise



def derive_bank_target(currency: str) -> str:
    """Route currency to bank via config.ccy_bank_index. Unknown → MANUAL."""
    from config import get_accounts
    c = (currency or "").upper()
    return get_accounts()["ccy_bank_index"].get(c, "MANUAL")


def upsert_job(job_id: str, **kwargs):
    """Insert job if new, otherwise update only supplied columns."""
    unknown = set(kwargs) - _JOB_COLUMNS
    if unknown:
        raise ValueError(f"Unknown job columns: {unknown}")
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
    from config import get_accounts
    allowed = set(get_accounts()["banks"]) | {"MANUAL"}
    if bank_target not in allowed:
        raise ValueError(f"Invalid bank_target: {bank_target!r}. Must be one of {sorted(allowed)}")
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
