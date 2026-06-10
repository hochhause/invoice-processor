"""
config.py — Per-bank, per-currency account configuration from environment.

Reads:  DEBTOR_NAME
        {BANK}_CURRENCIES   comma-separated list of currencies the bank handles
        {BANK}_DEFAULT_CCY  fallback account currency (e.g. CHF for BKB)
        {BANK}_{CCY}_IBAN   IBAN for that account
        {BANK}_{CCY}_BIC    BIC  for that account

Exposes: load_accounts() → raw dict (no cache)
         get_accounts()  → cached parse (call freely; reset via _clear_cache())
         resolve_account(bank, ccy) → {iban, bic} or None
"""
import os
import re
import logging

log = logging.getLogger(__name__)

_ACCT_KEY_RE = re.compile(r"^(?P<bank>[A-Z]+)_(?P<ccy>[A-Z]{3})_(IBAN|BIC)$")

# MOD-97 helpers — duplicated from pipeline to avoid circular import once T2
# makes db.py import config.py (config→pipeline→db→config would be circular).
def _norm_iban(s):
    return re.sub(r"[^A-Za-z0-9]", "", s).upper()


def _validate_iban(iban):
    iban = _norm_iban(iban)
    if len(iban) < 5:
        return False
    rearranged = iban[4:] + iban[:4]
    numeric = "".join(str(ord(c) - 55) if c.isalpha() else c for c in rearranged)
    return int(numeric) % 97 == 1


def load_accounts():
    """
    Parse env → accounts config dict (no caching — always re-reads os.environ).

    Returns:
        {
          "debtor_name":   str,
          "banks":         [str, ...],          ordered by first discovery
          "accounts":      {bank: {ccy: {iban, bic}}},
          "currencies":    {bank: set[ccy]},    all ccys claimed by that bank
          "defaults":      {bank: default_ccy},
          "ccy_bank_index":{ccy: bank},         first-bank-wins on collision
        }
    """
    debtor_name = os.getenv("DEBTOR_NAME", "")

    # 1. discover banks by scanning for *_CURRENCIES keys
    banks = []
    currencies = {}
    for key, val in os.environ.items():
        if not key.endswith("_CURRENCIES") or not val:
            continue
        bank = key[: -len("_CURRENCIES")]
        if not re.match(r"^[A-Z]+$", bank):
            continue
        banks.append(bank)
        currencies[bank] = {c.strip().upper() for c in val.split(",") if c.strip()}

    # 2. default ccy per bank
    defaults = {bank: os.getenv(f"{bank}_DEFAULT_CCY", "").upper() for bank in banks}

    # 3. per-bank per-ccy IBAN / BIC
    accounts = {bank: {} for bank in banks}
    for key, val in os.environ.items():
        m = _ACCT_KEY_RE.match(key)
        if not m:
            continue
        bank = m.group("bank")
        ccy = m.group("ccy")
        field = m.group(3).lower()  # "iban" or "bic"
        if bank not in accounts:
            continue
        if ccy not in accounts[bank]:
            accounts[bank][ccy] = {"iban": "", "bic": ""}
        accounts[bank][ccy][field] = val or ""

    # 4. ccy → bank index (first-bank-wins on collision)
    ccy_bank_index = {}
    for bank in banks:
        for ccy in currencies.get(bank, set()):
            if ccy in ccy_bank_index:
                log.warning("ccy %s claimed by %s and %s; keeping %s",
                            ccy, ccy_bank_index[ccy], bank, ccy_bank_index[ccy])
            else:
                ccy_bank_index[ccy] = bank

    # 5. warn when no banks discovered — all uploads will route to MANUAL
    if not banks:
        log.warning(
            "No bank accounts configured. Set {BANK}_CURRENCIES env keys (e.g. BKB_CURRENCIES=CHF,EUR,SEK). "
            "All currency routing will fall back to MANUAL until configured."
        )

    # 6. startup IBAN validation (fail-fast log — not raised; missing IBAN is fine)
    for bank, accts in accounts.items():
        for ccy, acct in accts.items():
            iban = acct.get("iban", "")
            if iban and not _validate_iban(iban):
                log.error("invalid IBAN MOD-97: %s_%s_IBAN=%r — fix .env before exporting",
                          bank, ccy, iban)

    return {
        "debtor_name":    debtor_name,
        "banks":          banks,
        "accounts":       accounts,
        "currencies":     currencies,
        "defaults":       defaults,
        "ccy_bank_index": ccy_bank_index,
    }


_accounts_cache = None


def get_accounts():
    """Cached accounts config — parses env once on first call."""
    global _accounts_cache
    if _accounts_cache is None:
        _accounts_cache = load_accounts()
    return _accounts_cache


def _clear_cache():
    """Reset the module-level cache. Call in tests before patching os.environ."""
    global _accounts_cache
    _accounts_cache = None


def resolve_account(bank, ccy, cfg=None):
    """
    Return {iban, bic, ccy} for (bank, ccy), where the returned ``ccy`` is the
    *account* currency actually resolved — equal to the requested ``ccy``, or
    the bank's DEFAULT_CCY when the request fell back.

    Falls back to bank's DEFAULT_CCY account when no ccy-specific account is
    configured (e.g. SEK invoice debits BKB-CHF IBAN — same IBAN, different
    payment currency → returned ccy is "CHF").  Returns None if bank is not in
    config or no account resolves.

    ``cfg`` is an accounts-config dict (as from load_accounts()/get_accounts());
    it defaults to the cached get_accounts(). Passing it explicitly lets callers
    (e.g. xml_export.build_pain001) resolve against a known config without
    touching module-global state — one source of truth, no drift.
    """
    cfg = cfg if cfg is not None else get_accounts()
    if bank not in cfg["accounts"]:
        return None
    bank_accts = cfg["accounts"][bank]
    if ccy in bank_accts:
        return {**bank_accts[ccy], "ccy": ccy}
    default_ccy = cfg["defaults"].get(bank, "")
    if default_ccy and default_ccy in bank_accts:
        return {**bank_accts[default_ccy], "ccy": default_ccy}
    return None
