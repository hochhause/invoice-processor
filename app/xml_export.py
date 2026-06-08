"""
xml_export.py — ISO 20022 pain.001.001.03 payment initiation XML generator.

Produces a CustomerCreditTransferInitiation message compatible with:
  - Swiss domestic (CHF): SIC / euroSIC via Swiss banks (BLKB, Raiffeisen, BKB, UBS, PostFinance)
  - SEPA (EUR): SEPA Credit Transfer (all EU/EEA banks)
  - Cross-border (GBP, USD, JPY, etc.): SWIFT via IBAN + BIC

Required env vars (debtor = the company sending payments):
  DEBTOR_NAME  — company name
  DEBTOR_IBAN  — company IBAN
  DEBTOR_BIC   — company BIC/SWIFT
"""
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from collections import defaultdict


_NS = "urn:iso:std:iso:20022:tech:xsd:pain.001.001.03"
_XSI = "http://www.w3.org/2001/XMLSchema-instance"
_SCHEMA_LOC = f"{_NS} pain.001.001.03.xsd"

# Service levels removed — now determined by _get_service_level(ccy, bank)


def _get_service_level(ccy: str, bank: str) -> tuple[str, bool]:
    """
    Determine service level code and whether to add SWIFT local instrument.
    Returns: (service_level_code, should_add_swift)
    """
    if bank == "BKB":
        if ccy == "CHF":
            return ("NURG", False)
        elif ccy == "SEK":
            return ("NURG", True)
        elif ccy == "EUR":
            return ("SEPA", False)
        else:
            return ("NURG", False)
    elif bank == "RAIFFEISEN":
        if ccy in ("USD", "CAD", "GBP"):
            return ("NURG", True)
        else:
            return ("NURG", False)
    else:
        # MANUAL or unknown bank
        return ("NURG", False)


def _sub(parent, tag, text=None):
    el = ET.SubElement(parent, tag)
    if text is not None:
        el.text = str(text)
    return el


def build_pain001(jobs: list[dict], debtor: dict, bank: str = None) -> str:
    """
    Build pain.001.001.03 XML from a list of job dicts.

    Args:
        jobs: list of job dicts from db.get_all_jobs()
        debtor: dict with keys 'name', 'iban', 'bic'
        bank: optional filter by bank_target ("BKB" | "RAIFFEISEN" | "MANUAL")

    Returns:
        UTF-8 XML string

    Raises:
        ValueError: if no payable jobs after filtering
    """
    # Filter: only done jobs with at least an IBAN (BIC optional for Swiss QR)
    payable = [
        j for j in jobs
        if j.get("status") in ("QR-processed", "LLM-Done")
        and j.get("iban", "").strip()
    ]

    # Filter by bank_target if bank specified
    if bank:
        payable = [j for j in payable if j.get("bank_target") == bank]

    if not payable:
        raise ValueError(f"No payable jobs (bank={bank})")

    now = datetime.now(timezone.utc)
    msg_id = f"LYFE{now.strftime('%Y%m%d%H%M%S')}"
    creation_dt = now.strftime("%Y-%m-%dT%H:%M:%SZ")

    # Group by currency for separate PmtInf blocks
    by_currency: dict[str, list[dict]] = defaultdict(list)
    for j in payable:
        ccy = (j.get("currency") or "CHF").upper()
        by_currency[ccy].append(j)

    total_txs = len(payable)
    ctrl_sum = sum(_parse_amount(j.get("amount", "0")) for j in payable)

    # Build XML tree
    root = ET.Element("Document", {
        "xmlns": _NS,
        "xmlns:xsi": _XSI,
        "xsi:schemaLocation": _SCHEMA_LOC,
    })
    cti = _sub(root, "CstmrCdtTrfInitn")

    # Group Header
    grp = _sub(cti, "GrpHdr")
    _sub(grp, "MsgId", msg_id)
    _sub(grp, "CreDtTm", creation_dt)
    _sub(grp, "NbOfTxs", total_txs)
    _sub(grp, "CtrlSum", f"{ctrl_sum:.2f}")
    initg = _sub(grp, "InitgPty")
    _sub(initg, "Nm", debtor.get("name", "Unknown")[:140])

    # One PmtInf per currency
    for idx, (ccy, txs) in enumerate(by_currency.items()):
        pmt_sum = sum(_parse_amount(j.get("amount", "0")) for j in txs)
        earliest = _earliest_date(txs)

        pmt = _sub(cti, "PmtInf")
        _sub(pmt, "PmtInfId", f"{msg_id}-{idx+1:02d}")
        _sub(pmt, "PmtMtd", "TRF")
        _sub(pmt, "NbOfTxs", len(txs))
        _sub(pmt, "CtrlSum", f"{pmt_sum:.2f}")

        pmt_tp = _sub(pmt, "PmtTpInf")
        svc = _sub(pmt_tp, "SvcLvl")
        svc_level, add_swift = _get_service_level(ccy, bank or "BKB")
        _sub(svc, "Cd", svc_level)
        if add_swift:
            lcl = _sub(pmt_tp, "LclInstrm")
            _sub(lcl, "Cd", "SWIFT")

        _sub(pmt, "ReqdExctnDt", earliest)

        # Debtor
        dbtr = _sub(pmt, "Dbtr")
        _sub(dbtr, "Nm", debtor.get("name", "Unknown")[:140])
        dbtr_acct = _sub(pmt, "DbtrAcct")
        dbtr_id = _sub(dbtr_acct, "Id")
        _sub(dbtr_id, "IBAN", debtor.get("iban", ""))
        if ccy == "CHF":
            _sub(dbtr_acct, "Ccy", "CHF")

        dbtr_agt = _sub(pmt, "DbtrAgt")
        dbtr_fin = _sub(dbtr_agt, "FinInstnId")
        _sub(dbtr_fin, "BIC", debtor.get("bic", ""))

        # One CdtTrfTxInf per invoice
        for j in txs:
            _add_tx(pmt, j, ccy)

    ET.indent(root, space="  ")
    xml_bytes = ET.tostring(root, encoding="unicode", xml_declaration=False)
    return f'<?xml version="1.0" encoding="UTF-8"?>\n{xml_bytes}'


def _add_tx(pmt: ET.Element, job: dict, ccy: str):
    tx = _sub(pmt, "CdtTrfTxInf")

    pmt_id = _sub(tx, "PmtId")
    end_to_end = (job.get("invoice_id") or "NOTPROVIDED")[:35]
    _sub(pmt_id, "EndToEndId", end_to_end)

    amt = _sub(tx, "Amt")
    instd = _sub(amt, "InstdAmt", f"{_parse_amount(job.get('amount','0')):.2f}")
    instd.set("Ccy", ccy)

    # Creditor agent — omit entirely if no BIC (Swiss QR domestic payments)
    bic = job.get("bic", "").strip()
    if bic:
        cdtr_agt = _sub(tx, "CdtrAgt")
        fin = _sub(cdtr_agt, "FinInstnId")
        _sub(fin, "BIC", bic[:11])

    # Creditor
    cdtr = _sub(tx, "Cdtr")
    _sub(cdtr, "Nm", (job.get("receiver") or "Unknown")[:140])

    # Creditor account
    cdtr_acct = _sub(tx, "CdtrAcct")
    cdtr_id = _sub(cdtr_acct, "Id")
    _sub(cdtr_id, "IBAN", job.get("iban", ""))

    # Remittance info
    ref = (job.get("reference") or job.get("invoice_id") or "")[:140]
    if ref:
        rmt = _sub(tx, "RmtInf")
        _sub(rmt, "Ustrd", ref)


def _parse_amount(s: str) -> float:
    try:
        return float(str(s).replace(",", ""))
    except (ValueError, TypeError):
        return 0.0


def _to_iso_date(d: str) -> str | None:
    """Convert dd.mm.yyyy or YYYY-MM-DD to YYYY-MM-DD, or None if unparseable."""
    if not d:
        return None
    for fmt, out in (("%d.%m.%Y", "%Y-%m-%d"), ("%Y-%m-%d", "%Y-%m-%d")):
        try:
            return datetime.strptime(d, fmt).strftime(out)
        except ValueError:
            pass
    return None


def _earliest_date(jobs: list[dict]) -> str:
    dates = [iso for j in jobs if (iso := _to_iso_date(j.get("due_date", "")))]
    if dates:
        return min(dates)
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


