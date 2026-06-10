"""
xml_export.py — ISO 20022 pain.001.001.09 payment initiation XML generator.

Validation target: app/schemas/pain.001.001.09.ch.03.xsd (SIX CH-restricted).
The targetNamespace is the plain ISO urn (root type Document_pain001_ch), so the
output namespace is unchanged from generic .09; only the schemaLocation filename
points at the CH file. Breaking deltas vs .03: FinInstnId BIC→BICFI (Dbtr+Cdtr
agents), ReqdExctnDt wraps a DateAndDateTime2Choice (<Dt>), namespace .03→.09.

Produces a CustomerCreditTransferInitiation message compatible with:
  - Swiss domestic (CHF): SIC / euroSIC via Swiss banks (BLKB, Raiffeisen, BKB, UBS, PostFinance)
  - SEPA (EUR): SEPA Credit Transfer (all EU/EEA banks)
  - Cross-border (GBP, USD, JPY, etc.): SWIFT via IBAN + BIC

Debtor accounts are no longer a single global account; each currency PmtInf
debits a per-bank/per-currency account resolved from config.resolve_account
(see config.py + [[DECISIONS#Per-Account Debtor Model]]). DEBTOR_NAME names the
sending company; per-account IBAN/BIC come from {BANK}_{CCY}_IBAN/BIC env keys.
"""
import re
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from collections import defaultdict
from decimal import Decimal, InvalidOperation

import config


_NS = "urn:iso:std:iso:20022:tech:xsd:pain.001.001.09"
_XSI = "http://www.w3.org/2001/XMLSchema-instance"
_SCHEMA_LOC = f"{_NS} pain.001.001.09.ch.03.xsd"

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


_ISO2_RE = re.compile(r'^[A-Z]{2}$')


def _iso2(value: str) -> str:
    """Return value if it is a valid ISO 3166-1 alpha-2 code, else empty string.
    Silently drops anything that is not exactly two ASCII uppercase letters —
    truncating a full country name produces a wrong-but-valid XSD code."""
    v = (value or "").strip().upper()
    return v if _ISO2_RE.match(v) else ""


def _sub(parent, tag, text=None):
    el = ET.SubElement(parent, tag)
    if text is not None:
        el.text = str(text)
    return el


def build_pain001(jobs: list[dict], accounts: dict, bank: str = None) -> str:
    """
    Build pain.001.001.09 XML from a list of job dicts.

    Args:
        jobs: list of job dicts from db.get_all_jobs()
        accounts: account-config dict (config.load_accounts()/get_accounts()) —
            supplies the debtor name plus the per-(bank, ccy) IBAN/BIC, resolved
            via config.resolve_account(bank, ccy, accounts).
        bank: bank_target to build for ("BKB" | "RAIFFEISEN" | "MANUAL"); each
            currency PmtInf debits resolve_account(bank, ccy) — so SEK and CHF
            blocks can legitimately share the BKB-CHF account (default fallback).

    Returns:
        UTF-8 XML string

    Raises:
        ValueError: if no payable jobs after filtering, or a payable currency has
            no resolvable debtor account (IBAN+BIC) under `bank` — export gating
            (T9) is expected to catch this upstream.
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
    debtor_name = (accounts.get("debtor_name") or "Unknown")[:140]
    initg = _sub(grp, "InitgPty")
    _sub(initg, "Nm", debtor_name)

    # One PmtInf per (transaction) currency
    for idx, (ccy, txs) in enumerate(by_currency.items()):
        # Resolve the debtor account for this bank+ccy. SEK (and any BKB ccy
        # without its own account) falls back to the bank default (CHF) — same
        # IBAN, different payment currency (FX). DbtrAgt is mandatory in .09 and
        # BICFI must match the pattern, so an account without IBAN+BIC cannot
        # produce valid XML → raise (T9 gates this before export).
        acct = config.resolve_account(bank, ccy, accounts)
        if not acct or not acct.get("iban") or not acct.get("bic"):
            raise ValueError(
                f"no debtor account for {bank}/{ccy} — set {bank}_{ccy}_IBAN/BIC "
                f"or a {bank} default account (see T9 export gating)"
            )

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

        # .09: ReqdExctnDt is a DateAndDateTime2Choice — wrap the date in <Dt>
        reqd = _sub(pmt, "ReqdExctnDt")
        _sub(reqd, "Dt", earliest)

        # Debtor — per-account: DbtrAcct/Ccy is the *account* currency (CHF for a
        # SEK→CHF fallback), distinct from the per-tx InstdAmt/Ccy (payment ccy).
        dbtr = _sub(pmt, "Dbtr")
        _sub(dbtr, "Nm", debtor_name)
        dbtr_acct = _sub(pmt, "DbtrAcct")
        dbtr_id = _sub(dbtr_acct, "Id")
        _sub(dbtr_id, "IBAN", acct["iban"])
        _sub(dbtr_acct, "Ccy", acct["ccy"])

        dbtr_agt = _sub(pmt, "DbtrAgt")
        dbtr_fin = _sub(dbtr_agt, "FinInstnId")
        _sub(dbtr_fin, "BICFI", acct["bic"])

        # ChrgBr — PmtInf level; XSD-optional, bank-required for SEPA/SWIFT
        # SEPA (EUR): SLEV (service-level charges); SWIFT cross-border: SHAR
        # CHF domestic: omitted (bank does not require it for SIC/euroSIC)
        if svc_level == "SEPA":
            _sub(pmt, "ChrgBr", "SLEV")
        elif add_swift:
            _sub(pmt, "ChrgBr", "SHAR")

        # One CdtTrfTxInf per invoice
        for j in txs:
            _add_tx(pmt, j, ccy)

    ET.indent(root, space="  ")
    xml_bytes = ET.tostring(root, encoding="unicode", xml_declaration=False)
    return f'<?xml version="1.0" encoding="UTF-8"?>\n{xml_bytes}'


def _cdtr_address(cdtr: ET.Element, job: dict) -> None:
    """
    Emit Cdtr/PstlAdr as pure-structured fields (no AdrLine).
    Emits only when at least one cdtr_* field is non-empty.
    Element order matches PostalAddress24 XSD sequence.
    """
    fields = [
        ("StrtNm", (job.get("cdtr_street")      or "").strip()[:70]),
        ("BldgNb", (job.get("cdtr_building_no") or "").strip()[:16]),
        ("PstCd",  (job.get("cdtr_postcode")    or "").strip()[:16]),
        ("TwnNm",  (job.get("cdtr_town")        or "").strip()[:35]),
        ("Ctry",   _iso2(job.get("cdtr_country"))),
    ]
    present = [(tag, val) for tag, val in fields if val]
    if not present:
        return
    adr = _sub(cdtr, "PstlAdr")
    for tag, val in present:
        _sub(adr, tag, val)


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
        _sub(fin, "BICFI", bic[:11])

    # Creditor — Nm + structured PstlAdr (no AdrLine per CH design)
    cdtr = _sub(tx, "Cdtr")
    _sub(cdtr, "Nm", (job.get("receiver") or "Unknown")[:140])
    _cdtr_address(cdtr, job)

    # Creditor account
    cdtr_acct = _sub(tx, "CdtrAcct")
    cdtr_id = _sub(cdtr_acct, "Id")
    _sub(cdtr_id, "IBAN", job.get("iban", ""))

    # Remittance info
    ref = (job.get("reference") or job.get("invoice_id") or "")[:140]
    if ref:
        rmt = _sub(tx, "RmtInf")
        _sub(rmt, "Ustrd", ref)


def _parse_amount(s: str) -> Decimal:
    """Parse amount to Decimal. Handles EU decimal comma ("1234,50") and
    thousands comma ("1,234.56"). LLM output uses decimal point per prompt."""
    text = str(s).strip()
    if re.search(r',\d{2}$', text):
        # EU format: last comma is decimal separator
        text = text.replace('.', '').replace(',', '.')
    else:
        text = text.replace(',', '')
    try:
        return Decimal(text)
    except InvalidOperation:
        return Decimal('0')


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


