// vendors.js — vendor IBAN database modal: list, inline-edit, add, delete

function _ve(s) {
  return String(s || '').replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
}

async function showVendors() {
  document.getElementById('vendors-modal').style.display = 'flex';
  await _loadVendors();
}

function closeVendors() {
  document.getElementById('vendors-modal').style.display = 'none';
}

async function _loadVendors() {
  const tbody = document.getElementById('vendor-rows');
  tbody.innerHTML = '<tr><td colspan="4" class="vendor-empty">Loading…</td></tr>';
  try {
    const vendors = await fetch('/api/vendors').then(r => r.json());
    if (!vendors.length) {
      tbody.innerHTML = '<tr><td colspan="4" class="vendor-empty">No vendors yet — add one below.</td></tr>';
      return;
    }
    tbody.innerHTML = vendors.map(_vendorRow).join('');
  } catch {
    tbody.innerHTML = '<tr><td colspan="4" class="vendor-empty" style="color:var(--red)">Failed to load vendors.</td></tr>';
  }
}

function _vendorRow(v) {
  return `<tr id="vrow-${v.id}" class="vendor-row">
    <td class="vendor-cell">${_ve(v.receiver_name)}</td>
    <td class="vendor-cell vendor-iban">${_ve(v.iban)}</td>
    <td class="vendor-cell vendor-bic">${_ve(v.bic || '')}</td>
    <td class="vendor-cell vendor-actions">
      <button class="btn-icon"
        data-vid="${v.id}"
        data-receiver="${_ve(v.receiver_name)}"
        data-iban="${_ve(v.iban)}"
        data-bic="${_ve(v.bic || '')}"
        onclick="editVendorRow(this)" title="Edit">✎</button>
      <button class="btn-icon danger" onclick="deleteVendor('${v.id}')" title="Delete">✕</button>
    </td>
  </tr>`;
}

function editVendorRow(btn) {
  const id = btn.dataset.vid;
  const row = document.getElementById(`vrow-${id}`);
  if (!row) return;
  row.innerHTML = `
    <td class="vendor-cell"><input id="ve-r-${id}" value="${btn.dataset.receiver}" class="vendor-input"></td>
    <td class="vendor-cell"><input id="ve-i-${id}" value="${btn.dataset.iban}" class="vendor-input vendor-iban"></td>
    <td class="vendor-cell"><input id="ve-b-${id}" value="${btn.dataset.bic}" class="vendor-input vendor-bic"></td>
    <td class="vendor-cell vendor-actions">
      <button class="btn btn-primary btn-sm" onclick="saveVendorRow('${id}')">Save</button>
      <button class="btn btn-ghost btn-sm" onclick="_loadVendors()">✕</button>
    </td>`;
  document.getElementById(`ve-r-${id}`)?.focus();
}

async function saveVendorRow(id) {
  const receiver = document.getElementById(`ve-r-${id}`)?.value.trim();
  const iban     = document.getElementById(`ve-i-${id}`)?.value.trim();
  const bic      = document.getElementById(`ve-b-${id}`)?.value.trim();
  if (!receiver || !iban) return;
  await fetch(`/api/vendors/${id}`, {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ receiver_name: receiver, iban, bic }),
  });
  await _loadVendors();
}

async function deleteVendor(id) {
  if (!confirm('Remove this vendor?')) return;
  await fetch(`/api/vendors/${id}`, { method: 'DELETE' });
  await _loadVendors();
}

async function addVendorRow() {
  const receiver = document.getElementById('va-receiver')?.value.trim();
  const iban     = document.getElementById('va-iban')?.value.trim();
  const bic      = document.getElementById('va-bic')?.value.trim();
  if (!receiver || !iban) {
    document.getElementById(receiver ? 'va-iban' : 'va-receiver')?.focus();
    return;
  }
  await fetch('/api/vendors', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ receiver_name: receiver, iban, bic }),
  });
  document.getElementById('va-receiver').value = '';
  document.getElementById('va-iban').value = '';
  document.getElementById('va-bic').value = '';
  await _loadVendors();
}

// Enter key in add-form submits
document.addEventListener('DOMContentLoaded', () => {
  ['va-receiver', 'va-iban', 'va-bic'].forEach(id => {
    document.getElementById(id)?.addEventListener('keydown', e => {
      if (e.key === 'Enter') addVendorRow();
    });
  });
});

window.showVendors  = showVendors;
window.closeVendors = closeVendors;
window.editVendorRow = editVendorRow;
window.saveVendorRow = saveVendorRow;
window.deleteVendor  = deleteVendor;
window.addVendorRow  = addVendorRow;
window._loadVendors  = _loadVendors;
