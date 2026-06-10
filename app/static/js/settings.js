// settings.js — first-run API key prompt (desktop builds only).
// Server mode (env-managed key) never shows the modal: status.desktop=false.

async function checkApiKeySetup() {
  try {
    const s = await fetch('/api/settings/status').then(r => r.json());
    if (s.desktop && !s.api_key_set) {
      document.getElementById('apikey-modal').style.display = 'flex';
      document.getElementById('apikey-input').focus();
    }
  } catch { /* non-fatal — dashboard still usable for QR-only flow */ }
}

async function saveApiKey() {
  const input = document.getElementById('apikey-input');
  const errEl = document.getElementById('apikey-error');
  const key = input.value.trim();
  errEl.style.display = 'none';
  if (!key) {
    errEl.textContent = 'Please paste the key first.';
    errEl.style.display = 'block';
    return;
  }
  const res = await fetch('/api/settings/api-key', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ api_key: key }),
  });
  if (res.ok) {
    document.getElementById('apikey-modal').style.display = 'none';
  } else {
    const d = await res.json().catch(() => ({}));
    errEl.textContent = d.error || 'Could not save the key.';
    errEl.style.display = 'block';
  }
}

document.getElementById('apikey-input')?.addEventListener('keydown', e => {
  if (e.key === 'Enter') saveApiKey();
});

checkApiKeySetup();
