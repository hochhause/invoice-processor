// dashboard.js — table polling, upload zone, row actions, LLM batch button

const POLL_STATUSES = new Set(['LLM-Pending', 'needs_review']);

let pollTimer = null;

// ── Upload zone (topbar button + whole-page drag-drop) ───────────────────────

const fileInput = document.getElementById('file-input');
const dropOverlay = document.body;

document.addEventListener('dragover', e => {
  e.preventDefault();
  document.body.classList.add('drag-active');
});
document.addEventListener('dragleave', e => {
  if (e.relatedTarget === null) document.body.classList.remove('drag-active');
});
document.addEventListener('drop', e => {
  e.preventDefault();
  document.body.classList.remove('drag-active');
  if (e.dataTransfer.files.length) uploadFiles(e.dataTransfer.files);
});
if (fileInput) fileInput.addEventListener('change', () => uploadFiles(fileInput.files));

async function uploadFiles(files) {
  const pdfs = Array.from(files).filter(f => f.name.toLowerCase().endsWith('.pdf'));
  if (!pdfs.length) return;

  const wrap = document.getElementById('upload-progress-wrap');
  const fill = document.getElementById('upload-progress-fill');
  const text = document.getElementById('upload-progress-text');
  if (wrap) wrap.style.display = 'block';

  for (let i = 0; i < pdfs.length; i++) {
    const fd = new FormData();
    fd.append('files', pdfs[i]);
    try {
      await fetch('/api/upload', { method: 'POST', body: fd });
    } catch (e) {
      console.error('[dashboard] upload failed:', e);
    }
    if (fill) fill.style.width = `${Math.round(((i + 1) / pdfs.length) * 100)}%`;
    if (text) text.textContent = `${i + 1} of ${pdfs.length} uploaded`;
  }

  if (fileInput) fileInput.value = '';
  setTimeout(() => { if (wrap) wrap.style.display = 'none'; }, 800);
  refreshJobs();
}

// ── Polling ───────────────────────────────────────────────────────────────────

async function refreshJobs() {
  try {
    const r = await fetch('/api/jobs');
    const jobs = await r.json();
    window._jobsCache = jobs;
    renderTable(jobs);
    updateSummary(jobs);
    updateTopbar(jobs);
    schedulePoll(jobs);
  } catch (e) {
    console.error('[dashboard] refreshJobs failed:', e);
  }
}

function schedulePoll(jobs) {
  const needsPoll = jobs.some(j => POLL_STATUSES.has(j.status));
  if (needsPoll && !pollTimer) {
    pollTimer = setInterval(refreshJobs, 2000);
  } else if (!needsPoll && pollTimer) {
    clearInterval(pollTimer);
    pollTimer = null;
  }
}

// ── Summary & topbar ─────────────────────────────────────────────────────────

function updateSummary(jobs) {
  const el = document.getElementById('summary-text');
  if (!el) return;
  if (!jobs.length) { el.innerHTML = 'No invoices yet'; return; }
  const counts = {
    total: jobs.length,
    qr: jobs.filter(j => j.status === 'QR-processed').length,
    pending: jobs.filter(j => j.status === 'LLM-Pending').length,
    done: jobs.filter(j => j.status === 'LLM-Done').length,
    review: jobs.filter(j => j.status === 'needs_review').length,
    error: jobs.filter(j => j.status === 'error').length,
  };
  const parts = [`<strong>${counts.total}</strong> total`];
  if (counts.qr) parts.push(`<strong>${counts.qr}</strong> QR`);
  if (counts.pending) parts.push(`<strong>${counts.pending}</strong> pending`);
  if (counts.done) parts.push(`<strong>${counts.done}</strong> done`);
  if (counts.review) parts.push(`<strong>${counts.review}</strong> review`);
  if (counts.error) parts.push(`<strong>${counts.error}</strong> error`);
  el.innerHTML = parts.join(' · ');
}

function updateTopbar(jobs) {
  const btnLlm = document.getElementById('btn-run-ai');
  if (btnLlm) {
    const hasPending = jobs.some(j => j.status === 'LLM-Pending');
    btnLlm.style.display = hasPending ? '' : 'none';
  }
}

// ── Table rendering ───────────────────────────────────────────────────────────

function renderTable(jobs) {
  const tbody = document.getElementById('job-rows');
  if (!tbody) return;
  tbody.innerHTML = jobs.map((job, idx) => buildRow(job, idx)).join('');
}

function buildRow(job, idx) {
  return `<tr class="invoice-row">
    <td class="cell-file" title="${esc(job.filename)}">${esc(job.filename)}</td>
    <td>${statusBadge(job.status)}</td>
    <td class="cell-receiver" title="${esc(job.receiver || '')}">${esc(job.receiver || '')}</td>
    <td class="cell-amount">${esc(job.amount || '')}</td>
    <td class="cell-currency">${esc(job.currency || '')}</td>
    <td class="cell-iban">${esc(job.iban || '')}</td>
    <td>${bankChip(job.bank_target || '')}</td>
    <td class="cell-actions">
      <button class="btn-icon" title="Edit" onclick="openModal('${job.id}', ${idx}, ${(window._jobsCache || []).length})">✎</button>
      <button class="btn-icon danger" title="Delete" onclick="deleteJob('${job.id}')">✕</button>
    </td>
  </tr>`;
}

function statusBadge(status) {
  const map = {
    'QR-processed': ['badge-qr', 'QR-Processed'],
    'LLM-Pending':  ['badge-llm-pending', 'LLM-Pending'],
    'LLM-Done':     ['badge-llm-done', 'LLM-Done'],
    'needs_review': ['badge-review', 'Needs Review'],
    'error':        ['badge-error', 'Error'],
    'archived':     ['badge-archived', 'Archived'],
  };
  const [cls, label] = map[status] || ['badge-archived', status];
  return `<span class="badge ${cls}">${label}</span>`;
}

function bankChip(bank) {
  if (!bank) return '';
  const map = { BKB: 'chip-bkb', RAIFFEISEN: 'chip-raiff', MANUAL: 'chip-manual' };
  const cls = map[bank] || '';
  return `<span class="chip ${cls}">${bank}</span>`;
}

function esc(s) {
  return String(s)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;');
}

// ── Actions ───────────────────────────────────────────────────────────────────

async function deleteJob(id) {
  if (!confirm('Remove this invoice?')) return;
  await fetch(`/api/jobs/${id}`, { method: 'DELETE' });
  refreshJobs();
}

async function clearAll() {
  if (!confirm('Delete ALL invoices? This cannot be undone.')) return;
  await fetch('/api/clear-all', { method: 'DELETE' });
  refreshJobs();
}

async function runAiBatch() {
  const btn = document.getElementById('btn-run-ai');
  if (btn) btn.innerHTML = '<span class="spinner"></span> Running…';
  try {
    const r = await fetch('/api/run-llm-batch', { method: 'POST' });
    const d = await r.json();
    if (btn) btn.textContent = `Queued ${d.queued}`;
  } catch (e) {
    console.error('[dashboard] run-llm-batch failed:', e);
    if (btn) btn.textContent = 'Error';
  }
  refreshJobs();
}

// ── Search & sort ─────────────────────────────────────────────────────────────

function filterTable() {
  const q = (document.getElementById('search-input')?.value || '').toUpperCase();
  document.querySelectorAll('#job-rows tr').forEach(row => {
    row.classList.toggle('hidden', !row.textContent.toUpperCase().includes(q));
  });
}

function sortTable(colIdx) {
  const table = document.getElementById('invoice-table');
  if (!table) return;
  const tbody = table.querySelector('tbody');
  const ths = table.querySelectorAll('thead th');
  const th = ths[colIdx];
  const isAsc = th.classList.contains('sort-asc');
  ths.forEach(h => h.classList.remove('sort-asc', 'sort-desc'));
  const rows = Array.from(tbody.querySelectorAll('tr:not(.hidden)'));
  rows.sort((a, b) => {
    const av = a.querySelectorAll('td')[colIdx]?.textContent.trim() || '';
    const bv = b.querySelectorAll('td')[colIdx]?.textContent.trim() || '';
    const cmp = av.localeCompare(bv, undefined, { numeric: true });
    return isAsc ? -cmp : cmp;
  });
  th.classList.add(isAsc ? 'sort-desc' : 'sort-asc');
  rows.forEach(r => tbody.appendChild(r));
}

// ── Init ──────────────────────────────────────────────────────────────────────

window.refreshJobs = refreshJobs;
refreshJobs();
