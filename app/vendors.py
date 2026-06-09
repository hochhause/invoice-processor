import db


def lookup(receiver: str) -> dict | None:
    """Case-insensitive exact match. Returns {iban, bic} or None. No fuzzy — false positives on IBAN are financial risk."""
    if not receiver:
        return None
    with db.get_db() as conn:
        row = conn.execute(
            "SELECT iban, bic FROM vendors WHERE lower(receiver_name) = lower(?)",
            (receiver.strip(),)
        ).fetchone()
        return dict(row) if row else None


def upsert_vendor(receiver_name: str, iban: str, bic: str = "") -> None:
    with db.get_db() as conn:
        existing = conn.execute(
            "SELECT id FROM vendors WHERE lower(receiver_name) = lower(?)",
            (receiver_name.strip(),)
        ).fetchone()
        if existing:
            conn.execute(
                "UPDATE vendors SET iban = ?, bic = ?, updated_at = datetime('now') WHERE id = ?",
                (iban, bic, existing["id"])
            )
        else:
            conn.execute(
                "INSERT INTO vendors (receiver_name, iban, bic) VALUES (?, ?, ?)",
                (receiver_name.strip(), iban, bic)
            )


def update_vendor(vendor_id: str, receiver_name: str, iban: str, bic: str = "") -> None:
    with db.get_db() as conn:
        conn.execute(
            "UPDATE vendors SET receiver_name = ?, iban = ?, bic = ?, updated_at = datetime('now') WHERE id = ?",
            (receiver_name.strip(), iban, bic, vendor_id)
        )


def list_vendors() -> list:
    with db.get_db() as conn:
        rows = conn.execute("SELECT * FROM vendors ORDER BY receiver_name").fetchall()
        return [dict(r) for r in rows]


def delete_vendor(vendor_id: str) -> None:
    with db.get_db() as conn:
        conn.execute("DELETE FROM vendors WHERE id = ?", (vendor_id,))
