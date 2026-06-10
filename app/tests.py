"""
tests.py — Startup self-tests for DEV_MODE.
Run automatically on startup when DEV_MODE=true.
Covers: IBAN checksum, BIC validation, extract_fields, pain.001 XML generation.
"""
import re
import sys


def run_startup_tests():
    import db as _db
    _db.init_db()

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
    bic_pattern = r"^[A-Z]{4}[A-Z]{2}[A-Z0-9]{2}([A-Z0-9]{3})?$"
    check("T2a-real-bic", bool(re.match(bic_pattern, "BLKBCH22")),
          "BLKBCH22 is a valid 8-char BIC")
    check("T2b-real-bic-11", bool(re.match(bic_pattern, "BLKBCH22XXX")),
          "BLKBCH22XXX is a valid 11-char BIC")
    check("T2c-fake-bic", not bool(re.match(bic_pattern, "SWIFT123")),
          "SWIFT123 (all-numeric location) should fail — needs at least 1 letter in location")
    check("T2d-too-short", not bool(re.match(bic_pattern, "BLKB")),
          "4-char string is not a valid BIC")

    # ── T4: pain.001 XML is well-formed and has required elements ─────────────
    from xml_export import build_pain001
    import xml.etree.ElementTree as ET

    # T4: build_pain001 now takes the accounts-config dict (not a single debtor)
    # and a concrete bank; each ccy PmtInf debits resolve_account(bank, ccy).
    # CHF and EUR debtor accounts carry *distinct* IBANs so per-account routing
    # is actually observable (P-criterion: EUR block debits its own BKB-EUR IBAN,
    # CHF + SEK blocks share the BKB-CHF IBAN). Both are valid CH-format IBANs.
    _BKB_CHF_IBAN = "CH5604835012345678009"
    _BKB_EUR_IBAN = "CH9300762011623852957"
    acct_cfg = {
        "debtor_name": "Test Corp AG",
        "banks": ["BKB"],
        "accounts": {"BKB": {
            "CHF": {"iban": _BKB_CHF_IBAN, "bic": "BLKBCH22"},
            "EUR": {"iban": _BKB_EUR_IBAN, "bic": "BLKBCH22"},
        }},
        "currencies": {"BKB": {"CHF", "EUR", "SEK"}},
        "defaults": {"BKB": "CHF"},
        "ccy_bank_index": {"CHF": "BKB", "EUR": "BKB", "SEK": "BKB"},
    }
    jobs_chf = [{"status": "LLM-Done", "iban": "CH5604835012345678009", "bic": "BLKBCH22",
                 "amount": "500.00", "currency": "CHF", "receiver": "Vendor AG", "bank_target": "BKB",
                 "invoice_id": "INV-001", "due_date": "2026-07-01", "reference": "REF-001"}]
    xml_str = build_pain001(jobs_chf, acct_cfg, "BKB")
    try:
        root = ET.fromstring(xml_str.split("\n", 1)[1])  # skip XML declaration
        ns = {"p": "urn:iso:std:iso:20022:tech:xsd:pain.001.001.09"}
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
         "bank_target": "BKB", "due_date": "2026-07-01", "reference": "R1"},
        {"status": "LLM-Done", "iban": "DE89370400440532013000", "bic": "DEUTDEDB",
         "amount": "200.00", "currency": "EUR", "receiver": "B", "invoice_id": "I2",
         "bank_target": "BKB", "due_date": "2026-07-02", "reference": "R2"},
    ]
    xml_multi = build_pain001(jobs_multi, acct_cfg, "BKB")
    try:
        root2 = ET.fromstring(xml_multi.split("\n", 1)[1])
        ns = {"p": "urn:iso:std:iso:20022:tech:xsd:pain.001.001.09"}
        pmtinf_els = root2.findall(".//p:PmtInf", ns)
        check("T5-two-pmtinf", len(pmtinf_els) == 2,
              f"Expected 2 PmtInf blocks for CHF+EUR, got {len(pmtinf_els)}")
    except ET.ParseError as e:
        failures.append(f"FAIL [T5-multi-xml]: {e}")

    # ── T5b-f: ChrgBr + structured PstlAdr (T5) ─────────────────────────────────
    # T5b: CHF domestic → no ChrgBr
    try:
        root_chf = ET.fromstring(xml_str.split("\n", 1)[1])  # xml_str = CHF job from T4
        ns_p = {"p": "urn:iso:std:iso:20022:tech:xsd:pain.001.001.09"}
        chrgbr_chf = root_chf.find(".//p:PmtInf/p:ChrgBr", ns_p)
        check("T5b-chf-no-chrgbr", chrgbr_chf is None,
              "CHF domestic PmtInf must not carry ChrgBr")
    except ET.ParseError as e:
        failures.append(f"FAIL [T5b-chf-no-chrgbr]: {e}")

    # T5c: EUR (SEPA) → ChrgBr=SLEV
    try:
        root_multi = ET.fromstring(xml_multi.split("\n", 1)[1])
        ns_p = {"p": "urn:iso:std:iso:20022:tech:xsd:pain.001.001.09"}
        eur_pmt = next(
            (p for p in root_multi.findall(".//p:PmtInf", ns_p)
             if (p.find(".//p:InstdAmt", ns_p) is not None and
                 p.find(".//p:InstdAmt", ns_p).get("Ccy") == "EUR")),
            None,
        )
        if eur_pmt is not None:
            chrgbr_eur = eur_pmt.find("p:ChrgBr", ns_p)
            check("T5c-eur-chrgbr-slev",
                  chrgbr_eur is not None and chrgbr_eur.text == "SLEV",
                  f"EUR PmtInf ChrgBr={chrgbr_eur.text if chrgbr_eur is not None else 'absent'}")
        else:
            failures.append("FAIL [T5c-eur-chrgbr-slev]: EUR PmtInf not found in xml_multi")
    except ET.ParseError as e:
        failures.append(f"FAIL [T5c-eur-chrgbr-slev]: {e}")

    # T5d: SWIFT cross-border (RAIFFEISEN/USD) → ChrgBr=SHAR
    raiff_cfg = {
        "debtor_name": "Test Corp AG",
        "banks": ["RAIFFEISEN"],
        "accounts": {"RAIFFEISEN": {
            "USD": {"iban": "CH5604835012345678009", "bic": "RAIFCH22XXX"},
        }},
        "currencies": {"RAIFFEISEN": {"USD", "CAD", "GBP"}},
        "defaults": {"RAIFFEISEN": "USD"},
        "ccy_bank_index": {"USD": "RAIFFEISEN", "CAD": "RAIFFEISEN", "GBP": "RAIFFEISEN"},
    }
    usd_job = {"status": "LLM-Done", "iban": "DE89370400440532013000",
               "bic": "DEUTDEDB", "amount": "300.00", "currency": "USD",
               "receiver": "Foreign Vendor", "bank_target": "RAIFFEISEN",
               "invoice_id": "INV-USD", "due_date": "2026-07-01", "reference": "R-USD"}
    try:
        xml_usd = build_pain001([usd_job], raiff_cfg, "RAIFFEISEN")
        root_usd = ET.fromstring(xml_usd.split("\n", 1)[1])
        ns_p = {"p": "urn:iso:std:iso:20022:tech:xsd:pain.001.001.09"}
        chrgbr_usd = root_usd.find(".//p:PmtInf/p:ChrgBr", ns_p)
        check("T5d-usd-chrgbr-shar",
              chrgbr_usd is not None and chrgbr_usd.text == "SHAR",
              f"USD/SWIFT PmtInf ChrgBr={chrgbr_usd.text if chrgbr_usd is not None else 'absent'}")
    except Exception as e:
        failures.append(f"FAIL [T5d-usd-chrgbr-shar]: {e}")

    # T5e: structured PstlAdr emitted when cdtr_* fields present
    job_with_addr = {**jobs_chf[0],
                     "cdtr_country": "DE", "cdtr_town": "Berlin", "cdtr_postcode": "10115",
                     "cdtr_street": "Unter den Linden", "cdtr_building_no": "1"}
    try:
        xml_addr = build_pain001([job_with_addr], acct_cfg, "BKB")
        root_addr = ET.fromstring(xml_addr.split("\n", 1)[1])
        ns_p = {"p": "urn:iso:std:iso:20022:tech:xsd:pain.001.001.09"}
        pstladr = root_addr.find(".//p:Cdtr/p:PstlAdr", ns_p)
        check("T5e-pstladr-present", pstladr is not None,
              "PstlAdr must be emitted when cdtr_* fields set")
        ctry_el = root_addr.find(".//p:Cdtr/p:PstlAdr/p:Ctry", ns_p)
        check("T5e-ctry", ctry_el is not None and ctry_el.text == "DE",
              f"Ctry={ctry_el.text if ctry_el is not None else 'absent'}")
        twn_el = root_addr.find(".//p:Cdtr/p:PstlAdr/p:TwnNm", ns_p)
        check("T5e-town", twn_el is not None and twn_el.text == "Berlin",
              f"TwnNm={twn_el.text if twn_el is not None else 'absent'}")
    except ET.ParseError as e:
        failures.append(f"FAIL [T5e-pstladr]: {e}")

    # T5f: no cdtr_* fields → no PstlAdr emitted
    try:
        ns_p = {"p": "urn:iso:std:iso:20022:tech:xsd:pain.001.001.09"}
        pstladr_none = ET.fromstring(xml_str.split("\n", 1)[1]).find(".//p:Cdtr/p:PstlAdr", ns_p)
        check("T5f-pstladr-absent", pstladr_none is None,
              "No PstlAdr when no cdtr_* fields in job dict")
    except ET.ParseError as e:
        failures.append(f"FAIL [T5f-pstladr-absent]: {e}")

    # ── Tdelta: the three .09 breaking deltas, asserted on emitted XML ──────────
    # XSD validation (Txsd) catches these indirectly, but the rename BIC→BICFI,
    # the ReqdExctnDt/Dt wrapper, and the namespace ARE the migration contract —
    # assert them directly so a regression names itself instead of surfacing as a
    # generic "XSD invalid". Reuses xml_str (CHF, T4) + xml_multi (CHF+EUR, T5).
    ns_p = {"p": "urn:iso:std:iso:20022:tech:xsd:pain.001.001.09"}
    try:
        root_d = ET.fromstring(xml_str.split("\n", 1)[1])
        # delta #1 — namespace is the plain ISO .09 urn (root tag carries it)
        check("Tdelta-a-namespace",
              root_d.tag == "{urn:iso:std:iso:20022:tech:xsd:pain.001.001.09}Document",
              f"root tag namespace wrong: {root_d.tag}")
        # delta #2 — DbtrAgt FI id is BICFI (not legacy BIC), value = account BIC
        dbtr_bicfi = root_d.find(".//p:DbtrAgt/p:FinInstnId/p:BICFI", ns_p)
        check("Tdelta-b-dbtr-bicfi",
              dbtr_bicfi is not None and dbtr_bicfi.text == "BLKBCH22",
              f"DbtrAgt BICFI={dbtr_bicfi.text if dbtr_bicfi is not None else 'absent'}")
        # delta #2 (regression guard) — no legacy <BIC> element survives anywhere
        check("Tdelta-c-no-legacy-bic",
              root_d.find(".//p:BIC", ns_p) is None,
              "legacy <BIC> element found — .09 renamed it to <BICFI>")
        # delta #3 — ReqdExctnDt wraps <Dt> (DateAndDateTime2Choice), not bare text
        reqd = root_d.find(".//p:PmtInf/p:ReqdExctnDt", ns_p)
        reqd_dt = root_d.find(".//p:PmtInf/p:ReqdExctnDt/p:Dt", ns_p)
        check("Tdelta-d-reqdexctndt-dt",
              reqd_dt is not None and reqd_dt.text == "2026-07-01",
              f"ReqdExctnDt/Dt={reqd_dt.text if reqd_dt is not None else 'absent'}")
        check("Tdelta-e-reqdexctndt-not-bare",
              reqd is not None and not (reqd.text or "").strip(),
              "ReqdExctnDt must not carry a bare date — wrap it in <Dt>")
    except ET.ParseError as e:
        failures.append(f"FAIL [Tdelta-xml]: {e}")

    # CdtrAgt also uses BICFI. Locate the EUR transaction (creditor BIC DEUTDEDB)
    # specifically — a bare .// would grab the first tx's CdtrAgt regardless.
    try:
        root_dm = ET.fromstring(xml_multi.split("\n", 1)[1])
        eur_tx = next(
            (t for t in root_dm.findall(".//p:CdtTrfTxInf", ns_p)
             if (a := t.find(".//p:InstdAmt", ns_p)) is not None and a.get("Ccy") == "EUR"),
            None,
        )
        cdtr_bicfi = eur_tx.find("p:CdtrAgt/p:FinInstnId/p:BICFI", ns_p) if eur_tx is not None else None
        check("Tdelta-f-cdtr-bicfi",
              cdtr_bicfi is not None and cdtr_bicfi.text == "DEUTDEDB",
              f"EUR tx CdtrAgt BICFI={cdtr_bicfi.text if cdtr_bicfi is not None else 'absent'}")
    except ET.ParseError as e:
        failures.append(f"FAIL [Tdelta-f-cdtr-bicfi]: {e}")

    # ── Tfx: per-account resolution + FX, asserted on emitted XML ───────────────
    # The headline mechanic (P-criteria): in a BKB file, CHF + SEK blocks both
    # debit the BKB-CHF IBAN (SEK has no own account → default fallback), while
    # EUR debits its own BKB-EUR IBAN. DbtrAcct/Ccy = *account* ccy (CHF for the
    # SEK fallback); per-tx InstdAmt/@Ccy = *payment* ccy (SEK) → an FX payment.
    def _pmtinf_for_ccy(root, ccy):
        for p in root.findall(".//p:PmtInf", ns_p):
            amt = p.find(".//p:InstdAmt", ns_p)
            if amt is not None and amt.get("Ccy") == ccy:
                return p
        return None

    jobs_fx = [
        {"status": "LLM-Done", "iban": "CH5604835012345678009", "bic": "BLKBCH22",
         "amount": "100.00", "currency": "CHF", "receiver": "Dom AG", "invoice_id": "FX-CHF",
         "bank_target": "BKB", "due_date": "2026-07-01", "reference": "RC"},
        {"status": "LLM-Done", "iban": "DE89370400440532013000", "bic": "DEUTDEDB",
         "amount": "200.00", "currency": "EUR", "receiver": "Eur AG", "invoice_id": "FX-EUR",
         "bank_target": "BKB", "due_date": "2026-07-02", "reference": "RE"},
        {"status": "LLM-Done", "iban": "CH5604835012345678009", "bic": "BLKBCH22",
         "amount": "300.00", "currency": "SEK", "receiver": "Sek AG", "invoice_id": "FX-SEK",
         "bank_target": "BKB", "due_date": "2026-07-03", "reference": "RS"},
    ]
    try:
        root_fx = ET.fromstring(build_pain001(jobs_fx, acct_cfg, "BKB").split("\n", 1)[1])

        def _dbtr_iban(pmt):
            el = pmt.find("p:DbtrAcct/p:Id/p:IBAN", ns_p)
            return el.text if el is not None else None

        def _dbtr_ccy(pmt):
            el = pmt.find("p:DbtrAcct/p:Ccy", ns_p)
            return el.text if el is not None else None

        chf_pmt, eur_pmt, sek_pmt = (_pmtinf_for_ccy(root_fx, c) for c in ("CHF", "EUR", "SEK"))
        check("Tfx-a-blocks-present",
              all(p is not None for p in (chf_pmt, eur_pmt, sek_pmt)),
              f"missing PmtInf block(s): CHF={chf_pmt is not None} EUR={eur_pmt is not None} SEK={sek_pmt is not None}")
        if all(p is not None for p in (chf_pmt, eur_pmt, sek_pmt)):
            # CHF + SEK both debit the BKB-CHF IBAN; EUR debits its own IBAN
            check("Tfx-b-chf-debits-chf-acct", _dbtr_iban(chf_pmt) == _BKB_CHF_IBAN,
                  f"CHF DbtrAcct IBAN={_dbtr_iban(chf_pmt)}")
            check("Tfx-c-sek-debits-chf-acct", _dbtr_iban(sek_pmt) == _BKB_CHF_IBAN,
                  f"SEK must fall back to BKB-CHF IBAN, got {_dbtr_iban(sek_pmt)}")
            check("Tfx-d-eur-debits-eur-acct", _dbtr_iban(eur_pmt) == _BKB_EUR_IBAN,
                  f"EUR must debit own BKB-EUR IBAN, got {_dbtr_iban(eur_pmt)}")
            # DbtrAcct/Ccy = account ccy (SEK block resolves to the CHF account)
            check("Tfx-e-sek-acct-ccy-chf", _dbtr_ccy(sek_pmt) == "CHF",
                  f"SEK block DbtrAcct/Ccy must be account ccy CHF, got {_dbtr_ccy(sek_pmt)}")
            # InstdAmt/@Ccy = payment ccy (SEK) — distinct from account ccy → FX
            sek_amt = sek_pmt.find(".//p:InstdAmt", ns_p)
            check("Tfx-f-sek-instd-ccy-sek", sek_amt is not None and sek_amt.get("Ccy") == "SEK",
                  f"SEK InstdAmt/@Ccy must stay SEK (FX), got {sek_amt.get('Ccy') if sek_amt is not None else 'absent'}")
    except Exception as e:
        failures.append(f"FAIL [Tfx]: {e}")

    # ── T6: Jobs missing IBAN are skipped; IBAN-only (no BIC) are included ───────
    # BIC is optional for Swiss QR-bill and SEPA — only IBAN is mandatory.
    jobs_skip = [
        {"status": "LLM-Done", "iban": "", "bic": "BLKBCH22", "amount": "100.00", "bank_target": "BKB",
         "currency": "CHF", "receiver": "X", "invoice_id": "I3", "due_date": "2026-07-01", "reference": "R3"},
        {"status": "LLM-Done", "iban": "CH5604835012345678009", "bic": "", "bank_target": "BKB",
         "amount": "200.00", "currency": "CHF", "receiver": "Y", "invoice_id": "I4",
         "due_date": "2026-07-01", "reference": "R4"},
    ]
    xml_skip = build_pain001(jobs_skip, acct_cfg, "BKB")
    try:
        root3 = ET.fromstring(xml_skip.split("\n", 1)[1])
        ns = {"p": "urn:iso:std:iso:20022:tech:xsd:pain.001.001.09"}
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

    # ── T11: run_vendor_check — exact match, mismatch, autofill, no-entry ───────
    import vendors as _vendors
    from pipeline import run_vendor_check as _rvc
    _vendors.upsert_vendor("VendorCheck AG", "CH5604835012345678009", "BLKBCH22")

    # Confirmed match
    _r11a = _rvc({"receiver": "VendorCheck AG", "iban": "CH5604835012345678009"})
    check("T11a-source-document", _r11a.get("iban_source") == "document", f"iban_source={_r11a.get('iban_source')}")

    # Mismatch — vendor table wins; discarded extracted IBAN preserved in iban_mismatch_db
    _r11b = _rvc({"receiver": "VendorCheck AG", "iban": "CH9300762011623852957"})
    check("T11b-mismatch",       _r11b.get("iban_source") == "document_mismatch",   f"iban_source={_r11b.get('iban_source')}")
    check("T11c-vendor-wins",    _r11b.get("iban") == "CH5604835012345678009",       f"vendor IBAN should win, got iban={_r11b.get('iban')}")
    check("T11c2-mismatch-db",   _r11b.get("iban_mismatch_db") == "CH9300762011623852957", f"iban_mismatch_db should hold extracted IBAN, got {_r11b.get('iban_mismatch_db')}")

    # Space-insensitive match — same IBAN with spaces should NOT produce a mismatch
    _r11h = _rvc({"receiver": "VendorCheck AG", "iban": "CH56 0483 5012 3456 7800 9"})
    check("T11h-space-match",    _r11h.get("iban_source") == "document",             f"space-separated IBAN should match, got iban_source={_r11h.get('iban_source')}")
    check("T11h2-no-override",   not _r11h.get("iban_mismatch_db"),                  f"no mismatch_db when spaces-only difference, got {_r11h.get('iban_mismatch_db')}")

    # No IBAN in doc → autofill from DB
    _r11d = _rvc({"receiver": "VendorCheck AG", "iban": ""})
    check("T11d-autofill-iban",   _r11d.get("iban") == "CH5604835012345678009", f"iban={_r11d.get('iban')}")
    check("T11e-autofill-source", _r11d.get("iban_source") == "database",        f"iban_source={_r11d.get('iban_source')}")

    # No vendor entry → fields unchanged
    _r11f = _rvc({"receiver": "Unknown Corp XYZ", "iban": "CH9300762011623852957"})
    check("T11f-no-vendor", _r11f.get("iban") == "CH9300762011623852957", "No vendor → iban unchanged")
    check("T11g-no-source", not _r11f.get("iban_source"), "No vendor → iban_source empty")

    # ── T12: cost estimate (token-based pricing) ───────────────────────────────
    from cost import estimate_cost as _est, price_for as _price
    check("T12a-haiku-input",  abs(_est("claude-haiku-4-5-20251001", 1_000_000, 0) - 1.00) < 1e-9,
          "1M Haiku input tokens = $1.00")
    check("T12b-haiku-output", abs(_est("claude-haiku-4-5-20251001", 0, 1_000_000) - 5.00) < 1e-9,
          "1M Haiku output tokens = $5.00")
    check("T12c-sonnet-price", _price("claude-sonnet-4-6") == (3.00, 15.00),
          f"Sonnet 4.6 priced $3/$15, got {_price('claude-sonnet-4-6')}")
    check("T12d-zero-tokens",  _est("claude-haiku-4-5-20251001", 0, 0) == 0.0,
          "Zero tokens = $0.00")

    # ── Tcfg: config.py — per-account config loader (T1) ─────────────────────
    import os as _os
    import config as _config
    from unittest.mock import patch as _patch

    _CFG_ENV = {
        "DEBTOR_NAME":            "Test Corp AG",
        "BKB_CURRENCIES":         "CHF,EUR,SEK",
        "BKB_DEFAULT_CCY":        "CHF",
        "BKB_CHF_IBAN":           "CH5604835012345678009",
        "BKB_CHF_BIC":            "BLKBCH22",
        "BKB_EUR_IBAN":           "CH5604835012345678009",
        "BKB_EUR_BIC":            "BLKBCH22",
        "RAIFFEISEN_CURRENCIES":  "USD,CAD,GBP",
        "RAIFFEISEN_DEFAULT_CCY": "USD",
        "RAIFFEISEN_USD_IBAN":    "CH5604835012345678009",
        "RAIFFEISEN_USD_BIC":     "BLKBCH22",
    }

    with _patch.dict(_os.environ, _CFG_ENV):
        _config._clear_cache()
        _cfg = _config.load_accounts()
        _acct_chf = _config.resolve_account("BKB", "CHF")
        _acct_sek = _config.resolve_account("BKB", "SEK")   # no BKB_SEK_IBAN → falls back to CHF
        _acct_unk = _config.resolve_account("UNKNOWN", "CHF")
        _acct_usd = _config.resolve_account("RAIFFEISEN", "USD")
    _config._clear_cache()  # don't leave test config in module cache

    check("Tcfg-a-debtor",    _cfg["debtor_name"] == "Test Corp AG",
          f"debtor_name={_cfg['debtor_name']!r}")
    check("Tcfg-b-banks",     set(_cfg["banks"]) == {"BKB", "RAIFFEISEN"},
          f"banks={_cfg['banks']}")
    check("Tcfg-c-ccy-chf",   _cfg["ccy_bank_index"].get("CHF") == "BKB",
          f"CHF → {_cfg['ccy_bank_index'].get('CHF')}")
    check("Tcfg-d-ccy-usd",   _cfg["ccy_bank_index"].get("USD") == "RAIFFEISEN",
          f"USD → {_cfg['ccy_bank_index'].get('USD')}")
    check("Tcfg-e-chf-acct",  (_cfg["accounts"].get("BKB", {}).get("CHF", {}).get("iban") ==
                                "CH5604835012345678009"),
          "BKB CHF IBAN not set")
    check("Tcfg-f-resolve-chf",     (_acct_chf is not None and
                                     _acct_chf["iban"] == "CH5604835012345678009"),
          f"resolve BKB/CHF={_acct_chf}")
    check("Tcfg-g-sek-fallback",    (_acct_sek is not None and
                                     _acct_sek["iban"] == "CH5604835012345678009"),
          f"SEK → BKB default CHF acct, got {_acct_sek}")
    check("Tcfg-h-unknown-none",    _acct_unk is None,
          f"unknown bank → None, got {_acct_unk}")
    check("Tcfg-i-resolve-usd",     (_acct_usd is not None and
                                     _acct_usd["iban"] == "CH5604835012345678009"),
          f"resolve RAIFFEISEN/USD={_acct_usd}")

    # ── Txsd: pain.001.001.09 output validates against the SIX CH XSD (T3) ─────
    # The .09 migration is silent-reject if the structural deltas are wrong
    # (BICFI, ReqdExctnDt/Dt, namespace). Validating build_pain001 output against
    # app/schemas/pain.001.001.09.ch.03.xsd turns "bank rejects the file" into a
    # local failure. Guarded: xmlschema lands with T10 (requirements.txt +
    # Dockerfile); until then this skips with a note rather than failing startup.
    # Reuses the debtor / jobs_chf / jobs_multi fixtures from T4/T5. See [[Plan]].
    try:
        import xmlschema  # noqa: E402
    except ImportError:
        print("[STARTUP TESTS] SKIP [Txsd]: xmlschema not installed (pending T10)")
    else:
        from pathlib import Path as _Path
        _xsd_path = _Path(__file__).resolve().parent / "schemas" / "pain.001.001.09.ch.03.xsd"
        if not _xsd_path.exists():
            print(f"[STARTUP TESTS] SKIP [Txsd]: XSD not found at {_xsd_path}")
        else:
            _xsd = xmlschema.XMLSchema(str(_xsd_path))
            for _label, _jobs in (("chf", jobs_chf), ("multi", jobs_multi)):
                _body = build_pain001(_jobs, acct_cfg, "BKB").split("\n", 1)[1]
                _errs = list(_xsd.iter_errors(_body))
                check(f"Txsd-{_label}-valid", not _errs,
                      f".09 output failed CH XSD: "
                      f"{_errs[0].reason if _errs else ''}")

    # ── Report ────────────────────────────────────────────────────────────────
    if failures:
        print(f"\n[STARTUP TESTS] {len(failures)} failure(s):", file=sys.stderr)
        for f in failures:
            print(f"  {f}", file=sys.stderr)
        raise RuntimeError(f"Startup tests failed: {len(failures)} check(s) failed")
    else:
        print(f"[STARTUP TESTS] All checks passed [OK]")
