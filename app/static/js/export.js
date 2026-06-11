// ── STATE ──
let jobs = [];
let blockedIds = new Set();  // job IDs with unresolvable account or cross-border gap
let acctSummary = {};        // {BANK: {default_ccy, resolve:{ccy:acct_ccy}}} — which acct each ccy debits
let bankList = [];           // configured banks, ordered (excludes MANUAL)

function _boardColumns() {
  return [...bankList, 'MANUAL'];
}

function esc(s) {
  return String(s == null ? '' : s)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;');
}

// needs_review always lands in unsorted column regardless of bank_target.
// A bank_target no longer in the config (bank removed via settings) also
// falls back to MANUAL so the card stays visible and re-assignable.
function _effectiveBank(job) {
  if (job.status === 'needs_review') return 'MANUAL';
  const b = job.bank_target || 'MANUAL';
  return b === 'MANUAL' || bankList.includes(b) ? b : 'MANUAL';
}

// ── READINESS HELPERS ──
async function fetchBlockedIds() {
  try {
    const res = await fetch('/api/export-readiness');
    if (!res.ok) return;
    const data = await res.json();
    blockedIds = new Set((data.blockers || []).map(b => b.id));
  } catch (err) {
    console.error('[export] readiness fetch failed:', err);
  }
}

// Resolve which debtor-account currency a (bank, payment-ccy) block debits.
// Mirrors config.resolve_account: explicit ccy account, else bank default.
function _acctFor(bank, ccy) {
  const s = acctSummary[bank];
  if (!s) return null;
  return (s.resolve && s.resolve[ccy]) || s.default_ccy || null;
}

async function fetchAccountsSummary() {
  try {
    acctSummary = await fetchBanksSummary();  // banks.js — shared cache
    bankList = Object.keys(acctSummary);
  } catch (err) {
    console.error('[export] accounts-summary fetch failed:', err);
  }
}

// ── INIT ──
async function initExport() {
  try {
    const [jobsRes] = await Promise.all([
      fetch('/api/jobs'),
      fetchBlockedIds(),
      fetchAccountsSummary(),
    ]);
    if (!jobsRes.ok) throw new Error(`Failed to fetch jobs: ${jobsRes.status}`);
    jobs = await jobsRes.json();
    window._jobsCache = jobs;  // modal.js reads from window._jobsCache
    buildBoard();
    render();
  } catch (err) {
    console.error('[export] init failed:', err);
    showToast('⚠ Failed to load invoices', 3000);
  }
}

// ── BOARD ──
// One column per configured bank (from /api/accounts-summary) + Unsorted, so
// banks added in the settings popup get a drop zone without code changes.
function buildBoard() {
  const board = document.getElementById('board');
  board.innerHTML = _boardColumns().map(bank => {
    const manual = bank === 'MANUAL';
    const color = manual ? 'var(--amber)' : bankColor(bank, bankList);
    const meta = manual
      ? 'Needs manual assignment'
      : Object.keys((acctSummary[bank] || {}).resolve || {}).join(' · ') || '—';
    return `
    <div class="bank-col">
      <div class="bank-header" style="border-top:3px solid ${color}">
        <div class="bank-name">${esc(bankLabel(bank))}</div>
        <div class="bank-meta">${esc(meta)}</div>
        <div class="bank-total" id="total-${esc(bank)}">—</div>
      </div>
      <div class="drop-zone" id="zone-${esc(bank)}" ondragover="onDragOver(event)"
           ondrop="onDrop(event,'${esc(bank)}')" ondragleave="onDragLeave(event)"></div>
    </div>`;
  }).join('');
}

// ── RENDER ──
function render() {
  _boardColumns().forEach(bank => {
    const el = document.getElementById(`zone-${bank}`);
    if (!el) return;
    el.innerHTML = '';
    jobs.filter(j => _effectiveBank(j) === bank).forEach(job => {
      el.appendChild(makeCard(job));
    });
  });
  updateStats();
}

function makeCard(job) {
  const notReady = job.status === 'needs_review';
  const blocked = blockedIds.has(job.id);
  const locked = notReady || blocked;
  const lockTitle = notReady
    ? 'Fix issues before export'
    : 'Account or cross-border data missing — resolve before export';

  const div = document.createElement('div');
  div.className = locked ? 'invoice-card locked' : 'invoice-card';
  div.draggable = !locked;
  div.dataset.id = job.id;

  const statusColor =
    job.status === 'qr_done' ? '#22BAA0' :
    job.status === 'llm_done' ? '#3b82f6' :
    job.status === 'needs_review' ? '#ef4444' :
    '#6b9e99';

  const lockHint = locked ? `<span class="card-lock" title="${lockTitle}">🔒</span>` : '';

  div.innerHTML = `
    <div class="card-top">
      <div class="card-receiver">${esc(job.receiver) || '—'}</div>
      <div class="card-amount">${esc(job.amount) || '—'}</div>
    </div>
    <div class="card-bottom">
      <span class="card-currency">${esc(job.currency) || '?'}</span>
      <span class="card-status" style="color:${statusColor}">● ${esc(job.status).replace(/_/g, ' ')}</span>
      ${lockHint}
    </div>
    <button class="card-open-btn" title="Edit" data-job-id="${esc(job.id)}">⤢</button>
  `;

  if (!locked) {
    div.addEventListener('dragstart', e => {
      e.dataTransfer.effectAllowed = 'move';
      e.dataTransfer.setData('text/plain', job.id);
      setTimeout(() => div.classList.add('dragging'), 0);
    });
    div.addEventListener('dragend', () => div.classList.remove('dragging'));
  }

  // open modal on card or button click (pass index so modal.js can populate form)
  div.addEventListener('click', () => {
    const idx = jobs.findIndex(j => j.id === job.id);
    window.openModal(job.id, idx, jobs.length);
  });

  return div;
}

// ── DRAG & DROP ──
function onDragOver(e) {
  e.preventDefault();
  e.currentTarget.classList.add('drag-over');
}
function onDragLeave(e) {
  e.currentTarget.classList.remove('drag-over');
}
async function onDrop(e, bank) {
  e.preventDefault();
  e.currentTarget.classList.remove('drag-over');
  const id = e.dataTransfer.getData('text/plain');
  const job = jobs.find(j => j.id === id);
  if (!job) return;

  try {
    const res = await fetch(`/api/assign-bank/${id}`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ bank_target: bank })
    });
    if (!res.ok) throw new Error(`Failed to assign bank: ${res.status}`);
    job.bank_target = bank;
    await fetchBlockedIds();  // reassignment may change resolvability
    render();
  } catch (err) {
    console.error('[export] assign-bank failed:', err);
    showToast('⚠ Failed to assign bank', 3000);
  }
}

// ── STATS ──
function updateStats() {
  const counts = {}, totals = {};
  _boardColumns().forEach(b => { counts[b] = 0; totals[b] = {}; });

  jobs.forEach(j => {
    const bank = _effectiveBank(j);
    counts[bank]++;
    const cur = j.currency || '?';
    if (!totals[bank][cur]) totals[bank][cur] = 0;
    totals[bank][cur] += parseFloat(j.amount) || 0;
  });

  document.getElementById('bank-stats').innerHTML = _boardColumns()
    .map(b => `${esc(b === 'MANUAL' ? 'Unsorted' : bankLabel(b))}: <span>${counts[b]}</span>`)
    .join(' &nbsp;·&nbsp; ');
  document.getElementById('invoice-count').textContent = `${jobs.length} invoices`;

  // Per-currency sub-totals, grouped by the debtor account each block debits.
  // SEK + CHF both fall to the BKB-CHF account → shown under one "CHF acct" row,
  // SEK flagged ⇄ (FX: payment ccy ≠ account ccy). No config → flat per-ccy line.
  const fmt = amt => amt.toLocaleString('en-US', { maximumFractionDigits: 2, minimumFractionDigits: 2 });
  const formatTotal = (bank) => {
    const curEntries = Object.entries(totals[bank]);
    if (!curEntries.length) return '—';
    if (!acctSummary[bank]) {
      return curEntries.map(([cur, amt]) => `${esc(cur)} ${fmt(amt)}`).join(' · ');
    }
    const groups = {};  // accountCcy → [{ccy, amt}]
    curEntries.forEach(([cur, amt]) => {
      const acct = _acctFor(bank, cur) || cur;
      (groups[acct] = groups[acct] || []).push({ ccy: cur, amt });
    });
    return Object.entries(groups).map(([acct, items]) => {
      const parts = items
        .map(it => `${esc(it.ccy)} ${fmt(it.amt)}${it.ccy !== acct ? ' ⇄' : ''}`)
        .join(' · ');
      return `<div class="acct-sub"><span class="acct-tag">${esc(acct)} acct</span>${parts}</div>`;
    }).join('');
  };

  _boardColumns().forEach(bank => {
    const el = document.getElementById(`total-${bank}`);
    if (!el) return;
    if (bank === 'MANUAL') el.textContent = `${counts.MANUAL} invoices`;
    else el.innerHTML = formatTotal(bank);
  });

  // Show unsorted warning
  const warn = document.getElementById('unsorted-warn');
  if (counts.MANUAL > 0) {
    warn.textContent = `⚠ ${counts.MANUAL} unsorted — will not be exported`;
    warn.style.display = '';
  } else {
    warn.style.display = 'none';
  }
}

// ── EXPORT LOGIC ──
async function acceptAndDownload() {
  const unsorted = jobs.filter(j => _effectiveBank(j) === 'MANUAL').length;

  // If any unsorted, confirm
  if (unsorted > 0) {
    const names = bankList.map(bankLabel).join(' + ') || 'bank';
    const ok = confirm(`${unsorted} invoice(s) will NOT be exported.\n\nProceed with ${names} files?`);
    if (!ok) return;
  }

  // Check readiness
  try {
    const res = await fetch('/api/export-readiness');
    const data = await res.json();
    if (!data.ready) {
      showBlockersPopup(data.blockers);
      return;
    }
  } catch (err) {
    console.error('[export] readiness check failed:', err);
    showToast('⚠ Failed to check readiness', 3000);
    return;
  }

  // Download
  try {
    document.getElementById('download-btn').disabled = true;
    const res = await fetch('/download/confirm', { method: 'POST' });
    if (res.status === 409) {
      const data = await res.json();
      showBlockersPopup(data.blockers);
      document.getElementById('download-btn').disabled = false;
      return;
    }
    if (!res.ok) throw new Error(`Download failed: ${res.status}`);

    const blob = await res.blob();
    const a = document.createElement('a');
    a.href = URL.createObjectURL(blob);
    a.download = 'pain001_export.zip';
    a.click();

    showToast(`✓ ${jobs.length - unsorted} invoices archived`);
    setTimeout(() => { window.location = '/'; }, 2000);
  } catch (err) {
    console.error('[export] download failed:', err);
    showToast('⚠ Download failed', 3000);
    document.getElementById('download-btn').disabled = false;
  }
}

// ── BLOCKERS POPUP ──
function _blockerReasons(b) {
  if (b.blocker_type === 'unresolvable_account') {
    return ['No configured debtor account for this bank / currency'];
  }
  if (b.blocker_type === 'cross_border_incomplete') {
    return [`Cross-border missing: ${(b.missing || []).join(', ')}`];
  }
  const lines = [];
  if (b.status) lines.push(`Status: ${b.status}`);
  if (b.missing && b.missing.length) lines.push(`Missing: ${b.missing.map(esc).join(', ')}`);
  return lines;
}

function showBlockersPopup(blockers) {
  const list = document.getElementById('blockers-list');
  list.innerHTML = '';
  blockers.forEach(b => {
    const item = document.createElement('div');
    item.className = 'blocker-item';
    const reasons = _blockerReasons(b);
    item.innerHTML = `
      <div class="filename">${esc(b.filename)}</div>
      ${reasons.map(r => `<div class="reason">${esc(r)}</div>`).join('')}
    `;
    list.appendChild(item);
  });
  document.getElementById('blockers-popup').classList.add('show');
}

function closeBlockersPopup() {
  document.getElementById('blockers-popup').classList.remove('show');
}

// ── TOAST ──
function showToast(msg, duration = 2000) {
  const toast = document.getElementById('toast');
  toast.textContent = msg;
  toast.classList.add('show');
  setTimeout(() => toast.classList.remove('show'), duration);
}

// ── INIT ON LOAD ──
document.addEventListener('DOMContentLoaded', initExport);
