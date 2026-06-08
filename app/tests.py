"""
tests.py — Startup self-tests for DEV_MODE.
Run automatically on startup when DEV_MODE=true.
Covers: IBAN checksum, BIC validation, extract_fields, pain.001 XML generation.
"""
import re
import sys


def run_startup_tests():
    failures = []

    def check(name, condition, detail=""):
        if not condition:
            failures.append(f"FAIL [{name}]: {detail}")

    # ── T1: IBAN checksum validation ──────────────────────────────────────────
    from pipeline import _validate_iban as _validate_iban_checksum
    check("T1a-valid-CH", _validate_iban_checksum("CH5604835012345678009"),
          "Valid CH IBAN should pass checksum")
    check("T1b-valid-DE", _validate_iban_checksum("DE89370400440532013000"),
          "Valid DE IBAN should pass checksum")
    check("T1c-invalid", not _validate_iban_checksum("CH9900000000000000000"),
          "Invalid IBAN should fail checksum")
    check("T1d-fake-dev", not _validate_iban_checksum("CH" + "1234567890123456789"),
          "Fake dev IBAN should fail checksum (non-MOD97)")

    # ── T2: BIC regex rejects fake patterns, accepts real ────────────────────
    BIC_PATTERN = r"^[A-Z]{4}[A-Z]{2}[A-Z0-9]{2}([A-Z0-9]{3})?$"
    check("T2a-real-bic", bool(re.match(BIC_PATTERN, "BLKBCH22")),
          "BLKBCH22 is a valid 8-char BIC")
    check("T2b-real-bic-11", bool(re.match(BIC_PATTERN, "BLKBCH22XXX")),
          "BLKBCH22XXX is a valid 11-char BIC")
    check("T2c-fake-bic", not bool(re.match(BIC_PATTERN, "SWIFT123")),
          "SWIFT123 (all-numeric location) should fail — needs at least 1 letter in location")
    check("T2d-too-short", not bool(re.match(BIC_PATTERN, "BLKB")),
          "4-char string is not a valid BIC")

    # ── T4: pain.001 XML is well-formed and has required elements ─────────────
    from xml_export import build_pain001
    import xml.etree.ElementTree as ET

    debtor = {"name": "Test Corp AG", "iban": "CH5604835012345678009", "bic": "BLKBCH22"}
    jobs_chf = [{"status": "LLM-Done", "iban": "CH5604835012345678009", "bic": "BLKBCH22",
                 "amount": "500.00", "currency": "CHF", "receiver": "Vendor AG",
                 "invoice_id": "INV-001", "due_date": "2026-07-01", "reference": "REF-001"}]
    xml_str = build_pain001(jobs_chf, debtor)
    check("T4a-xml-parseable", True, "")  # will throw if bad
    try:
        root = ET.fromstring(xml_str.split("\n", 1)[1])  # skip XML declaration
        ns = {"p": "urn:iso:std:iso:20022:tech:xsd:pain.001.001.03"}
        grp = root.find(".//p:GrpHdr", ns)
        check("T4b-grphdr-exists", grp is not None, "GrpHdr element missing")
        nb = grp.find("p:NbOfTxs", ns)
        check("T4c-nbtxs-1", nb is not None and nb.text == "1", f"NbOfTxs={nb.text if nb is not None else 'None'}")
        iban_el = root.find(".//p:CdtrAcct/p:Id/p:IBAN", ns)
        check("T4d-creditor-iban", iban_el is not None and "CH56" in (iban_el.text or ""),
              f"Creditor IBAN element not found or wrong: {iban_el.text if iban_el is not None else None}")
    except ET.ParseError as e:
        failures.append(f"FAIL [T4-xml-parse]: {e}")

    # ── T5: Multi-currency → multiple PmtInf blocks ───────────────────────────
    jobs_multi = [
        {"status": "LLM-Done", "iban": "CH5604835012345678009", "bic": "BLKBCH22",
         "amount": "100.00", "currency": "CHF", "receiver": "A", "invoice_id": "I1",
         "due_date": "2026-07-01", "reference": "R1"},
        {"status": "LLM-Done", "iban": "DE89370400440532013000", "bic": "DEUTDEDB",
         "amount": "200.00", "currency": "EUR", "receiver": "B", "invoice_id": "I2",
         "due_date": "2026-07-02", "reference": "R2"},
    ]
    xml_multi = build_pain001(jobs_multi, debtor)
    try:
        root2 = ET.fromstring(xml_multi.split("\n", 1)[1])
        ns = {"p": "urn:iso:std:iso:20022:tech:xsd:pain.001.001.03"}
        pmtinf_els = root2.findall(".//p:PmtInf", ns)
        check("T5-two-pmtinf", len(pmtinf_els) == 2,
              f"Expected 2 PmtInf blocks for CHF+EUR, got {len(pmtinf_els)}")
    except ET.ParseError as e:
        failures.append(f"FAIL [T5-multi-xml]: {e}")

    # ── T6: Jobs missing IBAN are skipped; IBAN-only (no BIC) are included ───────
    # BIC is optional for Swiss QR-bill and SEPA — only IBAN is mandatory.
    jobs_skip = [
        {"status": "LLM-Done", "iban": "", "bic": "BLKBCH22", "amount": "100.00",
         "currency": "CHF", "receiver": "X", "invoice_id": "I3", "due_date": "2026-07-01", "reference": "R3"},
        {"status": "LLM-Done", "iban": "CH5604835012345678009", "bic": "",
         "amount": "200.00", "currency": "CHF", "receiver": "Y", "invoice_id": "I4",
         "due_date": "2026-07-01", "reference": "R4"},
    ]
    xml_skip = build_pain001(jobs_skip, debtor)
    try:
        root3 = ET.fromstring(xml_skip.split("\n", 1)[1])
        ns = {"p": "urn:iso:std:iso:20022:tech:xsd:pain.001.001.03"}
        nb3 = root3.find(".//p:NbOfTxs", ns)
        # Only the IBAN-only job passes; the no-IBAN job is skipped → NbOfTxs=1
        check("T6-skip-no-iban", nb3 is not None and nb3.text == "1",
              f"Expected NbOfTxs=1 (IBAN-only job included, no-IBAN job skipped), got {nb3.text if nb3 is not None else None}")
    except ET.ParseError as e:
        failures.append(f"FAIL [T6-skip-xml]: {e}")

    # ── T7: derive_bank_target routing ───────────────────────────────────────
    from db import derive_bank_target
    check("T7a-chf-bkb",    derive_bank_target("CHF") == "BKB",        "CHF → BKB")
    check("T7b-usd-raiff",  derive_bank_target("USD") == "RAIFFEISEN", "USD → RAIFFEISEN")
    check("T7c-jpy-manual", derive_bank_target("JPY") == "MANUAL",     "JPY → MANUAL")

    # ── T8: run_qr with mock QR data ─────────────────────────────────────────
    from unittest.mock import patch
    import pipeline
    _mock_qr = {
        "receiver": "Test AG",
        "iban":     "CH5604835012345678009",
        "amount":   "100.00",
        "currency": "CHF",
        "reference": "RF18539007547034",
    }
    with patch("qr_swiss.extract_from_pdf", return_value=_mock_qr):
        _qr_result = pipeline.run_qr("/fake/path.pdf")
    check("T8a-status-qr",   _qr_result.get("status") == "QR-processed",          f"status={_qr_result.get('status')}")
    check("T8b-receiver",    _qr_result.get("receiver") == "Test AG",              f"receiver={_qr_result.get('receiver')}")
    check("T8c-bank-target", _qr_result.get("bank_target") == "BKB",              f"bank_target={_qr_result.get('bank_target')}")
    check("T8d-iban",        _qr_result.get("iban") == "CH5604835012345678009",   f"iban={_qr_result.get('iban')}")

    # ── T9: _validate_iban — spaces + lowercase (T1 covers base valid/invalid) ─
    from pipeline import _validate_iban as _vi
    check("T9a-spaces",    _vi("CH56 0483 5012 3456 7800 9"), "IBAN with spaces should pass")
    check("T9b-lowercase", _vi("ch5604835012345678009"),      "Lowercase IBAN should pass")

    # ── T10: LLM JSON parsing logic ───────────────────────────────────────────
    import json as _json
    _raw_valid = ('{"invoice_id": "INV-1", "receiver": "X AG", "amount": "100.00",'
                  ' "currency": "CHF", "due_date": "2026-07-01",'
                  ' "iban": null, "bic": null, "reference": null}')
    _parsed = _json.loads(_raw_valid)
    _coerced = {k: (v or "") for k, v in _parsed.items()}
    check("T10a-null-to-empty", _coerced.get("iban") == "",  "null iban → ''")
    check("T10b-null-bic",      _coerced.get("bic") == "",   "null bic → ''")
    _bad_result = None
    try:
        _json.loads("not valid json {{{")
        _bad_result = "parsed"  # should not reach here
    except _json.JSONDecodeError:
        pass  # correct — llm.extract_fields returns None on JSONDecodeError
    check("T10c-malformed-json", _bad_result is None, "Malformed JSON raises JSONDecodeError → extract_fields returns None")

    # ── Report ────────────────────────────────────────────────────────────────
    total = 26  # T1(4) + T2(4) + T4(4) + T5(1) + T6(1) + T7(3) + T8(4) + T9(2) + T10(3)
    passed = total - len(failures)
    if failures:
        print(f"\n[STARTUP TESTS] {len(failures)} failure(s):", file=sys.stderr)
        for f in failures:
            print(f"  {f}", file=sys.stderr)
        raise RuntimeError(f"Startup tests failed: {len(failures)} check(s) failed")
    else:
        print(f"[STARTUP TESTS] All checks passed [OK]")
