// banks.js — shared bank list + deterministic colors (fetched once per page).
// Banks come from /api/accounts-summary (config-driven), so banks added via
// the settings popup appear everywhere (chips, pills, export board) without
// touching frontend code. MANUAL is always appended by callers where needed.

const BANK_PALETTE = ['#3b82f6', '#10b981', '#8b5cf6', '#ec4899', '#14b8a6', '#f97316'];
const MANUAL_COLOR = '#f59e0b';

let _banksPromise = null;

function fetchBanksSummary(force = false) {
  if (!_banksPromise || force) {
    _banksPromise = fetch('/api/accounts-summary')
      .then(r => (r.ok ? r.json() : {}))
      .then(s => {
        window._bankNames = Object.keys(s);
        return s;
      })
      .catch(() => ({}));
  }
  return _banksPromise;
}

function bankColor(bank, bankNames) {
  if (!bank || bank === 'MANUAL') return MANUAL_COLOR;
  const names = bankNames || window._bankNames || [];
  const i = names.indexOf(bank);
  return BANK_PALETTE[(i >= 0 ? i : 0) % BANK_PALETTE.length];
}

// BKB → "BKB", RAIFFEISEN → "Raiffeisen": short codes stay uppercase.
function bankLabel(bank) {
  if (bank === 'MANUAL') return 'Manual';
  return bank.length <= 4 ? bank : bank.charAt(0) + bank.slice(1).toLowerCase();
}

fetchBanksSummary();
