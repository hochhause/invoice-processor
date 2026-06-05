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
    field_statuses  TEXT DEFAULT '{}',
    error_msg   TEXT DEFAULT '',
    created_at  TEXT DEFAULT (datetime('now')),
    updated_at  TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS vendors (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT NOT NULL DEFAULT '',
    iban        TEXT NOT NULL DEFAULT '',
    invoice_ids TEXT NOT NULL DEFAULT '[]',
    source      TEXT DEFAULT '',
    created_at  TEXT DEFAULT (datetime('now')),
    updated_at  TEXT DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_vendors_iban ON vendors(iban);
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
        # Migration: add field_statuses column if missing
        cols = {row[1] for row in conn.execute("PRAGMA table_info(jobs)").fetchall()}
        if "field_statuses" not in cols:
            conn.execute("ALTER TABLE jobs ADD COLUMN field_statuses TEXT DEFAULT '{}'")
        # Migration: absorb old whitelist table into vendors
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
        if "whitelist" in tables:
            import json as _json
            rows = conn.execute("SELECT type, value, source FROM whitelist").fetchall()
            by_iban, by_receiver = {}, {}
            for r in rows:
                if r[0] == "iban":
                    by_iban[r[1]] = r[2]
                elif r[0] == "receiver":
                    by_receiver[r[1]] = r[2]
            # Pair up by source filename; unpaired get their own row
            for iban, src in by_iban.items():
                name = next((n for n, s in by_receiver.items() if s == src), "")
                conn.execute(
                    "INSERT INTO vendors (name, iban, source) VALUES (?, ?, ?)",
                    (name, iban, src),
                )
            for name, src in by_receiver.items():
                if not any(True for _ in conn.execute(
                    "SELECT 1 FROM vendors WHERE source = ? AND name = ?", (src, name)
                ).fetchall()):
                    conn.execute(
                        "INSERT INTO vendors (name, iban, source) VALUES (?, ?, ?)",
                        (name, "", src),
                    )
            conn.execute("DROP TABLE whitelist")
            print(f"[db] Migrated whitelist → vendors ({len(by_iban)} IBANs, {len(by_receiver)} receivers)", flush=True)



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


import json as _json


# ── Vendor CRUD ───────────────────────────────────────────────────────────────

def get_vendors() -> list:
    with get_db() as conn:
        rows = conn.execute("SELECT * FROM vendors ORDER BY name").fetchall()
    result = []
    for r in rows:
        v = dict(r)
        try:
            v["invoice_ids"] = _json.loads(v["invoice_ids"])
        except Exception:
            v["invoice_ids"] = []
        result.append(v)
    return result


def get_vendor(vendor_id: int) -> dict | None:
    with get_db() as conn:
        r = conn.execute("SELECT * FROM vendors WHERE id = ?", (vendor_id,)).fetchone()
    if not r:
        return None
    v = dict(r)
    try:
        v["invoice_ids"] = _json.loads(v["invoice_ids"])
    except Exception:
        v["invoice_ids"] = []
    return v


def upsert_vendor(vendor_id: int | None, name: str, iban: str, source: str = "") -> int:
    """Create or update a vendor row. Returns the row id."""
    with get_db() as conn:
        if vendor_id:
            conn.execute(
                "UPDATE vendors SET name=?, iban=?, source=?, updated_at=datetime('now') WHERE id=?",
                (name, iban, source, vendor_id),
            )
            return vendor_id
        cur = conn.execute(
            "INSERT INTO vendors (name, iban, source) VALUES (?, ?, ?)",
            (name, iban, source),
        )
        return cur.lastrowid


def delete_vendor(vendor_id: int):
    with get_db() as conn:
        conn.execute("DELETE FROM vendors WHERE id = ?", (vendor_id,))


def find_vendor(receiver: str = "", iban: str = "") -> dict | None:
    """Find a vendor matching receiver (case-insensitive substring) or IBAN (exact)."""
    with get_db() as conn:
        rows = conn.execute("SELECT * FROM vendors").fetchall()
    iban_norm = iban.replace(" ", "").upper() if iban else ""
    receiver_norm = receiver.strip().lower() if receiver else ""
    for r in rows:
        v = dict(r)
        v_iban = v["iban"].replace(" ", "").upper()
        v_name = v["name"].strip().lower()
        if iban_norm and v_iban and iban_norm == v_iban:
            try:
                v["invoice_ids"] = _json.loads(v["invoice_ids"])
            except Exception:
                v["invoice_ids"] = []
            return v
        if receiver_norm and v_name and (
            receiver_norm == v_name
            or receiver_norm in v_name
            or v_name in receiver_norm
        ):
            try:
                v["invoice_ids"] = _json.loads(v["invoice_ids"])
            except Exception:
                v["invoice_ids"] = []
            return v
    return None


def add_invoice_id_to_vendor(vendor_id: int, invoice_id: str, max_stored: int = 50):
    """Append an invoice_id to vendor history (dedup, keep latest max_stored)."""
    if not invoice_id:
        return
    with get_db() as conn:
        r = conn.execute("SELECT invoice_ids FROM vendors WHERE id = ?", (vendor_id,)).fetchone()
        if not r:
            return
        try:
            ids = _json.loads(r[0])
        except Exception:
            ids = []
        if invoice_id not in ids:
            ids.append(invoice_id)
            ids = ids[-max_stored:]  # keep latest
        conn.execute(
            "UPDATE vendors SET invoice_ids=?, updated_at=datetime('now') WHERE id=?",
            (_json.dumps(ids), vendor_id),
        )


# ── Legacy shim — keep pipeline.py happy until refactored ────────────────────

def is_whitelisted(type: str, value: str) -> bool:
    """Check if a receiver/iban exists in vendors."""
    if not value:
        return False
    if type == "iban":
        return find_vendor(iban=value) is not None
    if type == "receiver":
        return find_vendor(receiver=value) is not None
    return False


def add_to_whitelist(type: str, value: str, source: str = ""):
    """Legacy shim: add receiver/iban as a vendor stub if not already present."""
    if not value:
        return
    if type == "iban":
        if not find_vendor(iban=value):
            upsert_vendor(None, "", value, source)
    elif type == "receiver":
        if not find_vendor(receiver=value):
            upsert_vendor(None, value, "", source)


def get_whitelist(type: str = None) -> list:
    """Legacy shim for /api/whitelist endpoint."""
    return get_vendors()


def clear_whitelist(type: str = None):
    with get_db() as conn:
        conn.execute("DELETE FROM vendors")
