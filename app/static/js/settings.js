// settings.js — settings popup: API key, payee name, LLM model, banks/accounts.
// Everything that used to live in .env is editable here in desktop builds;
// server mode shows the values read-only (config stays env-managed there).
// First run (desktop, no API key) opens the popup automatically.

let _settings = null;  // last GET /api/settings payload

function escS(s) {
  return String(s == null ? '' : s)
    .replace(/&/g, '&amp;').replace(/</g, '&lt;')
    .replace(/>/g, '&gt;').replace(/"/g, '&quot;');
}

async function openSettings() {
  const modal = document.getElementById('settings-modal');
  modal.style.display = 'flex';
  document.getElementById('settings-error').style.display = 'none';
  try {
    _settings = await fetch('/api/settings').then(r => r.json());
  } catch {
    _settings = { desktop: false, api_key_set: false, debtor_name: '', llm_model: '', banks: [] };
  }
  _renderSettings();
}

function closeSettings() {
  document.getElementById('settings-modal').style.display = 'none';
}

function _renderSettings() {
  const s = _settings;
  document.getElementById('s-debtor').value = s.debtor_name || '';
  document.getElementById('s-model').value = s.llm_model || '';
  const keyInput = document.getElementById('s-apikey');
  keyInput.value = '';
  keyInput.placeholder = s.api_key_set ? '•••••••• (saved — paste to replace)' : 'sk-ant-…';

  const banksEl = document.getElementById('settings-banks');
  banksEl.innerHTML = '';
  (s.banks || []).forEach(b => banksEl.appendChild(_bankCard(b)));

  const readonly = !s.desktop;
  document.getElementById('settings-server-note').style.display = readonly ? '' : 'none';
  document.getElementById('settings-save').style.display = readonly ? 'none' : '';
  document.getElementById('settings-add-bank').style.display = readonly ? 'none' : '';
  document.querySelectorAll('#settings-modal input, #settings-modal button.s-del')
    .forEach(el => { el.disabled = readonly; });
}

function _acctRow(acct) {
  const row = document.createElement('div');
  row.className = 'settings-acct';
  row.style.cssText = 'display:flex;gap:6px;align-items:center;margin-top:6px';
  row.innerHTML = `
    <input class="sa-ccy" maxlength="3" placeholder="CCY" value="${escS(acct.ccy)}" style="width:52px;text-transform:uppercase">
    <input class="sa-iban" placeholder="IBAN (CH…)" value="${escS(acct.iban)}" style="flex:2">
    <input class="sa-bic" placeholder="BIC" value="${escS(acct.bic)}" style="flex:1">
    <button class="s-del" title="Remove account" onclick="this.parentElement.remove()"
            style="background:none;border:none;color:var(--text-dim);cursor:pointer;font-size:14px">✕</button>`;
  return row;
}

function _bankCard(bank) {
  const card = document.createElement('div');
  card.className = 'settings-bank';
  card.style.cssText = 'border:1px solid var(--border);border-radius:8px;padding:12px;margin-top:10px';
  card.innerHTML = `
    <div style="display:flex;gap:6px;align-items:center">
      <input class="sb-name" placeholder="BANK" value="${escS(bank.name)}"
             style="width:130px;font-weight:700;text-transform:uppercase">
      <span style="font-size:11px;color:var(--text-dim)">default</span>
      <input class="sb-default" maxlength="3" placeholder="CCY" value="${escS(bank.default_ccy)}"
             style="width:52px;text-transform:uppercase">
      <div style="flex:1"></div>
      <button class="s-del" title="Remove bank" onclick="this.closest('.settings-bank').remove()"
              style="background:none;border:none;color:var(--red);cursor:pointer;font-size:14px">✕</button>
    </div>
    <div style="margin-top:8px">
      <div style="font-size:11px;color:var(--text-dim);margin-bottom:4px">Currencies this bank handles (comma-separated)</div>
      <input class="sb-currencies" placeholder="CHF, EUR, SEK" value="${escS((bank.currencies || []).join(', '))}"
             style="width:100%;box-sizing:border-box;text-transform:uppercase">
    </div>
    <div style="margin-top:8px">
      <div style="font-size:11px;color:var(--text-dim)">Accounts (IBAN per currency; others fall back to the default account)</div>
      <div class="sb-accounts"></div>
      <button class="btn btn-ghost s-del" style="margin-top:8px;padding:3px 10px;font-size:12px"
              onclick="this.previousElementSibling.appendChild(_acctRow({ccy:'',iban:'',bic:''}))">+ Account</button>
    </div>`;
  const acctsEl = card.querySelector('.sb-accounts');
  (bank.accounts || []).forEach(a => acctsEl.appendChild(_acctRow(a)));
  return card;
}

function _addBank() {
  document.getElementById('settings-banks')
    .appendChild(_bankCard({ name: '', currencies: [], default_ccy: '', accounts: [{ ccy: '', iban: '', bic: '' }] }));
}

function _collectSettings() {
  return {
    debtor_name: document.getElementById('s-debtor').value.trim(),
    llm_model: document.getElementById('s-model').value.trim(),
    api_key: document.getElementById('s-apikey').value.trim(),
    banks: [...document.querySelectorAll('#settings-banks .settings-bank')].map(card => ({
      name: card.querySelector('.sb-name').value,
      default_ccy: card.querySelector('.sb-default').value,
      currencies: card.querySelector('.sb-currencies').value
        .split(',').map(s => s.trim()).filter(Boolean),
      accounts: [...card.querySelectorAll('.settings-acct')].map(r => ({
        ccy: r.querySelector('.sa-ccy').value,
        iban: r.querySelector('.sa-iban').value,
        bic: r.querySelector('.sa-bic').value,
      })).filter(a => a.ccy || a.iban || a.bic),
    })),
  };
}

async function saveSettings() {
  const errEl = document.getElementById('settings-error');
  errEl.style.display = 'none';
  const res = await fetch('/api/settings', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(_collectSettings()),
  });
  const d = await res.json().catch(() => ({}));
  if (!res.ok) {
    errEl.textContent = d.error || 'Could not save settings.';
    errEl.style.display = 'block';
    return;
  }
  _settings = d.settings;
  closeSettings();
  // Bank list / colors / routing may have changed — repaint everything.
  fetchBanksSummary(true).then(() => {
    if (typeof window.refreshJobs === 'function') window.refreshJobs();
    if (typeof window._renderBankPills === 'function') window._renderBankPills();
  });
}

// First-run: desktop build without an API key → open settings automatically.
async function checkFirstRun() {
  try {
    const s = await fetch('/api/settings/status').then(r => r.json());
    if (s.desktop && !s.api_key_set) openSettings();
  } catch { /* non-fatal — dashboard still usable for QR-only flow */ }
}

checkFirstRun();
