// ── STATE ──
let jobs = [];

function esc(s) {
  return String(s == null ? '' : s)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;');
}

// needs_review always lands in unsorted column regardless of bank_target
function _effectiveBank(job) {
  return job.status === 'needs_review' ? 'MANUAL' : (job.bank_target || 'MANUAL');
}

// ── INIT ──
async function initExport() {
  try {
    const response = await fetch('/api/jobs');
    if (!response.ok) throw new Error(`Failed to fetch jobs: ${response.status}`);
    jobs = await response.json();
    window._jobsCache = jobs;  // modal.js reads from window._jobsCache
    render();
  } catch (err) {
    console.error('[export] init failed:', err);
    showToast('⚠ Failed to load invoices', 3000);
  }
}

// ── RENDER ──
function render() {
  ['BKB', 'RAIFFEISEN', 'MANUAL'].forEach(bank => {
    const zoneId = bank === 'RAIFFEISEN' ? 'zone-raiff' : bank === 'BKB' ? 'zone-bkb' : 'zone-unsorted';
    const el = document.getElementById(zoneId);
    el.innerHTML = '';
    jobs.filter(j => _effectiveBank(j) === bank).forEach(job => {
      el.appendChild(makeCard(job));
    });
  });
  updateStats();
}

function makeCard(job) {
  const locked = job.status === 'needs_review';
  const div = document.createElement('div');
  div.className = locked ? 'invoice-card locked' : 'invoice-card';
  div.draggable = !locked;
  div.dataset.id = job.id;

  const statusColor =
    job.status === 'qr_done' ? '#22BAA0' :
    job.status === 'llm_done' ? '#3b82f6' :
    job.status === 'needs_review' ? '#ef4444' :
    '#6b9e99';

  const lockHint = locked ? `<span class="card-lock" title="Fix issues before export">🔒</span>` : '';

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
    render();
  } catch (err) {
    console.error('[export] assign-bank failed:', err);
    showToast('⚠ Failed to assign bank', 3000);
  }
}

// ── STATS ──
function updateStats() {
  const counts = { BKB: 0, RAIFFEISEN: 0, MANUAL: 0 };
  const totals = { BKB: {}, RAIFFEISEN: {}, MANUAL: {} };

  jobs.forEach(j => {
    const bank = _effectiveBank(j);
    counts[bank]++;
    const cur = j.currency || '?';
    if (!totals[bank][cur]) totals[bank][cur] = 0;
    totals[bank][cur] += parseFloat(j.amount) || 0;
  });

  document.getElementById('stat-bkb').textContent = counts.BKB;
  document.getElementById('stat-raiff').textContent = counts.RAIFFEISEN;
  document.getElementById('stat-unsorted').textContent = counts.MANUAL;
  document.getElementById('invoice-count').textContent = `${jobs.length} invoices`;

  // Format totals by currency per bank
  const formatTotal = (bank) => {
    const entries = Object.entries(totals[bank])
      .map(([cur, amt]) => `${cur} ${amt.toLocaleString('en-US', { maximumFractionDigits: 2, minimumFractionDigits: 2 })}`)
      .join(' · ');
    return entries || '—';
  };

  document.getElementById('bkb-total').textContent = formatTotal('BKB');
  document.getElementById('raiff-total').textContent = formatTotal('RAIFFEISEN');
  document.getElementById('unsorted-count').textContent = `${counts.MANUAL} invoices`;

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
    const ok = confirm(`${unsorted} invoice(s) will NOT be exported.\n\nProceed with BKB + Raiffeisen files?`);
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
function showBlockersPopup(blockers) {
  const list = document.getElementById('blockers-list');
  list.innerHTML = '';
  blockers.forEach(b => {
    const item = document.createElement('div');
    item.className = 'blocker-item';
    item.innerHTML = `
      <div class="filename">${esc(b.filename)}</div>
      <div class="reason">Status: ${esc(b.status)}</div>
      ${b.missing && b.missing.length > 0 ? `<div class="reason">Missing: ${b.missing.map(esc).join(', ')}</div>` : ''}
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
