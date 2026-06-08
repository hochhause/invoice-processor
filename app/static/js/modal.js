// modal.js — modal lifecycle: open/close/save/save-next, bank pills, field colors
// Reads from window._jobsCache (set by dashboard.js after each poll).
// Exports: window.openModal, window.closeModal

const MANDATORY_FIELDS = ['receiver', 'iban', 'amount', 'currency'];
const ALL_FIELDS = ['receiver', 'invoice_id', 'amount', 'currency', 'iban', 'bic', 'reference'];
const FIELD_IDS = {
  receiver: 'f-receiver',
  invoice_id: 'f-invoice-id',
  amount: 'f-amount',
  currency: 'f-currency',
  iban: 'f-iban',
  bic: 'f-bic',
  reference: 'f-reference',
};

let _modalJobIndex = 0;
let _modalJobList = [];  // snapshot used for Save & Next navigation

function openModal(jobId, jobIndex, totalJobs) {
  const jobs = window._jobsCache || [];
  const idx = jobIndex !== undefined ? jobIndex : jobs.findIndex(j => j.id === jobId);
  if (idx === -1 && jobIndex === undefined) return;

  _modalJobList = jobs;
  _modalJobIndex = idx !== -1 ? idx : 0;
  _populateModal(_modalJobList[_modalJobIndex]);
  document.getElementById('modal').classList.add('open');
}

function _populateModal(job) {
  if (!job) return;
  document.getElementById('modal-job-id').value = job.id;
  document.getElementById('pdf-frame').src = `/api/pdf/${job.id}`;
  document.getElementById('modal-title').textContent = job.filename || job.id;

  const total = _modalJobList.length;
  const navEl = document.getElementById('modal-nav');
  navEl.textContent = total > 1 ? `${_modalJobIndex + 1} of ${total}` : '';

  for (const field of ALL_FIELDS) {
    const el = document.getElementById(FIELD_IDS[field]);
    if (el) el.value = job[field] || '';
  }

  _colorFields();
  _updateBankPills(job.bank_target || '');
  _updateIbanSource(job);
}

function _colorFields() {
  for (const field of ALL_FIELDS) {
    const el = document.getElementById(FIELD_IDS[field]);
    if (!el) continue;
    el.classList.remove('ok', 'warn', 'err');
    const val = el.value.trim();
    if (val) {
      el.classList.add('ok');
    } else if (MANDATORY_FIELDS.includes(field)) {
      el.classList.add('err');
    } else {
      el.classList.add('warn');
    }
  }
}

function _updateIbanSource(job) {
  const badgeEl = document.getElementById('iban-source-badge');
  const mismatchEl = document.getElementById('iban-mismatch-warning');
  const mismatchDbEl = document.getElementById('iban-mismatch-db-value');
  const addVendorBtn = document.getElementById('btn-add-vendor');

  const badgeMap = {
    'document':          '<span class="chip chip-iban-doc">doc ✓</span>',
    'document_mismatch': '<span class="chip chip-iban-mismatch">doc ⚠</span>',
    'database':          '<span class="chip chip-iban-db">DB</span>',
    'llm':               '<span class="chip chip-iban-llm">llm</span>',
    'manual':            '<span class="chip chip-iban-manual">manual</span>',
  };

  if (badgeEl) badgeEl.innerHTML = badgeMap[job.iban_source || ''] || '';

  if (mismatchEl) {
    const isMismatch = job.iban_source === 'document_mismatch';
    mismatchEl.style.display = isMismatch ? '' : 'none';
    if (mismatchDbEl) mismatchDbEl.textContent = job.iban_mismatch_db || '';
  }

  if (addVendorBtn) {
    const src = job.iban_source || '';
    const alreadyKnown = src === 'document' || src === 'database';
    const showAdd = !alreadyKnown && job.iban;
    addVendorBtn.style.display = showAdd ? '' : 'none';
    addVendorBtn.textContent = src === 'document_mismatch' ? '↑ Update vendor IBAN' : '+ Save to vendors';
  }
}

async function addToVendors() {
  const jobId = document.getElementById('modal-job-id').value;
  if (!jobId) return;
  const receiver = document.getElementById('f-receiver')?.value || '';
  const iban = document.getElementById('f-iban')?.value || '';
  const bic = document.getElementById('f-bic')?.value || '';
  if (!receiver || !iban) return;

  await fetch('/api/vendors', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ receiver_name: receiver, iban, bic }),
  });

  const badgeEl = document.getElementById('iban-source-badge');
  if (badgeEl) badgeEl.innerHTML = '<span class="chip chip-iban-db">DB</span>';
  const btn = document.getElementById('btn-add-vendor');
  if (btn) btn.style.display = 'none';
}
window.addToVendors = addToVendors;

function _updateBankPills(bankTarget) {
  const map = { BKB: 'pill-bkb', RAIFFEISEN: 'pill-raiff', MANUAL: 'pill-manual' };
  for (const [bank, id] of Object.entries(map)) {
    const el = document.getElementById(id);
    if (!el) continue;
    el.className = 'bank-pill';
    if (bank === bankTarget.toUpperCase()) {
      if (bank === 'BKB') el.classList.add('active-bkb');
      else if (bank === 'RAIFFEISEN') el.classList.add('active-raiff');
      else if (bank === 'MANUAL') el.classList.add('active-manual');
    }
  }
}

function closeModal() {
  document.getElementById('modal').classList.remove('open');
  document.getElementById('pdf-frame').src = '';
}

function closeModalOnBackdrop(e) {
  if (e.target === e.currentTarget) closeModal();
}

async function saveModal() {
  const jobId = document.getElementById('modal-job-id').value;
  if (!jobId) return;

  const body = {};
  for (const field of ALL_FIELDS) {
    const el = document.getElementById(FIELD_IDS[field]);
    if (el) body[field] = el.value;
  }

  await fetch(`/api/review/${jobId}`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
    body: new URLSearchParams(body).toString(),
  });

  closeModal();
  if (typeof window.refreshJobs === 'function') window.refreshJobs();
}

async function saveAndNext() {
  const jobId = document.getElementById('modal-job-id').value;
  if (!jobId) return;

  const body = {};
  for (const field of ALL_FIELDS) {
    const el = document.getElementById(FIELD_IDS[field]);
    if (el) body[field] = el.value;
  }

  await fetch(`/api/review/${jobId}`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
    body: new URLSearchParams(body).toString(),
  });

  if (typeof window.refreshJobs === 'function') {
    await window.refreshJobs();
  }

  const nextIdx = (_modalJobIndex + 1) % (_modalJobList.length || 1);
  if (_modalJobList.length > 1) {
    _modalJobIndex = nextIdx;
    const updatedJobs = window._jobsCache || _modalJobList;
    _modalJobList = updatedJobs;
    const nextJob = updatedJobs[nextIdx] || updatedJobs[0];
    if (nextJob) _populateModal(nextJob);
  } else {
    closeModal();
  }
}

async function selectBankPill(bank) {
  const jobId = document.getElementById('modal-job-id').value;
  if (!jobId) return;

  _updateBankPills(bank);

  await fetch(`/api/assign-bank/${jobId}`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ bank_target: bank }),
  });

  if (typeof window.refreshJobs === 'function') window.refreshJobs();
}

function _wireInputListeners() {
  for (const id of Object.values(FIELD_IDS)) {
    const el = document.getElementById(id);
    if (el) el.addEventListener('input', _colorFields);
  }
}

// Expose globally (callable from dashboard.js and export.js)
window.openModal = openModal;
window.closeModal = closeModal;

// Wire input color listeners after DOM is ready
if (document.readyState === 'loading') {
  document.addEventListener('DOMContentLoaded', _wireInputListeners);
} else {
  _wireInputListeners();
}
