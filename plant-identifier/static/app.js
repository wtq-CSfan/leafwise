const fileInput = document.querySelector('#fileInput');
const preview = document.querySelector('#preview');
const uploadIcon = document.querySelector('#uploadIcon');
const uploadTitle = document.querySelector('#uploadTitle');
const uploadHint = document.querySelector('#uploadHint');
const statusBox = document.querySelector('#status');
const submitBtn = document.querySelector('#submitBtn');
const resultSection = document.querySelector('#resultSection');
const resultBox = document.querySelector('#result');
const historyList = document.querySelector('#historyList');
const cameraBtn = document.querySelector('#cameraBtn');
const uploadForm = document.querySelector('#uploadForm');
const clearHistoryBtn = document.querySelector('#clearHistoryBtn');
const installBtn = document.querySelector('#installBtn');
let selectedFile = null;
let installPrompt = null;

cameraBtn.addEventListener('click', () => fileInput.click());
fileInput.addEventListener('change', () => {
  selectedFile = fileInput.files && fileInput.files[0];
  statusBox.textContent = '';
  statusBox.className = 'status';
  if (!selectedFile) return;
  if (!selectedFile.type.startsWith('image/')) {
    showStatus('Please choose an image file.', true); selectedFile = null; return;
  }
  if (selectedFile.size > 10 * 1024 * 1024) {
    showStatus('The image must be 10 MB or smaller. Please compress it and try again.', true); selectedFile = null; return;
  }
  preview.src = URL.createObjectURL(selectedFile);
  preview.hidden = false; uploadIcon.hidden = true;
  uploadTitle.textContent = selectedFile.name;
  uploadHint.textContent = 'Image ready — you can start identification';
  resultSection.hidden = true;
});
function showStatus(message, isError = false) {
  statusBox.textContent = message; statusBox.className = isError ? 'status error' : 'status';
}
function escapeHtml(value) {
  return String(value ?? '').replace(/[&<>"']/g, char => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[char]));
}
function formatDate(value) {
  try { return new Date(value).toLocaleString(); } catch { return value || ''; }
}
uploadForm.addEventListener('submit', async event => {
  event.preventDefault();
  if (!selectedFile) { showStatus('Please take or choose a plant photo first.', true); return; }
  const oldText = submitBtn.innerHTML;
  submitBtn.disabled = true; submitBtn.textContent = 'Identifying…';
  showStatus('Uploading your photo and identifying the plant. Please wait.');
  const formData = new FormData(); formData.append('img', selectedFile);
  try {
    const response = await fetch('/upload', {method:'POST', body:formData});
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || 'Identification failed. Please try again.');
    renderResult(data, selectedFile);
    showStatus('Identification complete. Saved to your history.');
    await loadHistory();
  } catch (error) { showStatus(error.message || 'Request failed. Check your connection and try again.', true); }
  finally { submitBtn.disabled = false; submitBtn.innerHTML = oldText; }
});
function renderResult(data, file) {
  const best = (data.result || [])[0];
  if (!best) throw new Error('No valid identification result was returned.');
  const score = Number(best.score || 0) * 100;
  const description = best.baike_info?.description || 'An English description is not available for this plant.';
  const imageUrl = URL.createObjectURL(file);
  const baikeLink = best.baike_info?.baike_url ? `<p><a href="${escapeHtml(best.baike_info.baike_url)}" target="_blank" rel="noopener noreferrer">View original encyclopedia entry ↗</a></p>` : '';
  resultBox.innerHTML = `<img class="result-image" src="${imageUrl}" alt="Identified plant photo"><div><span class="eyebrow">IDENTIFIED PLANT</span><h3>${escapeHtml(best.name || 'Plant name unavailable in English')}</h3><span class="confidence">Match confidence ${score.toFixed(1)}%</span><p>${escapeHtml(description)}</p>${baikeLink}</div>`;
  resultSection.hidden = false; resultSection.scrollIntoView({behavior:'smooth', block:'start'});
}
async function loadHistory() {
  try {
    const response = await fetch('/history'); const data = await response.json(); const records = data.records || [];
    if (!records.length) { historyList.innerHTML = '<div class="empty-state">No plant records yet. Identify your first plant ✿</div>'; return; }
    historyList.innerHTML = records.map(record => `<article class="history-card">${record.image_data ? `<img src="${record.image_data}" alt="${escapeHtml(record.plant_name)}">` : ''}<div class="history-body"><div class="history-title-row"><h3>${escapeHtml(record.plant_name)}</h3><button class="delete-btn" data-delete="${record.id}" aria-label="Delete this record" title="Delete record">×</button></div><p>${escapeHtml((record.description || 'No English description available.').slice(0,130))}</p><div class="history-date">${formatDate(record.created_at)} · Match ${((Number(record.score)||0)*100).toFixed(1)}%</div></div></article>`).join('');
  } catch { historyList.innerHTML = '<div class="empty-state">Could not load history. Please refresh the page.</div>'; }
}
historyList.addEventListener('click', async event => {
  const button = event.target.closest('[data-delete]'); if (!button) return;
  if (!confirm('Delete this plant record?')) return;
  try { const response = await fetch(`/history/${button.dataset.delete}`, {method:'DELETE'}); const data = await response.json(); if (!response.ok) throw new Error(data.error || 'Could not delete the record.'); await loadHistory(); }
  catch (error) { alert(error.message || 'Could not delete the record. Please try again.'); }
});
clearHistoryBtn.addEventListener('click', async () => {
  if (!confirm('Clear all plant records? This action cannot be undone.')) return;
  try { const response = await fetch('/history', {method:'DELETE'}); if (!response.ok) throw new Error('Could not clear history.'); await loadHistory(); }
  catch (error) { alert(error.message || 'Could not clear history. Please try again.'); }
});
window.addEventListener('beforeinstallprompt', event => { event.preventDefault(); installPrompt = event; installBtn.hidden = false; });
installBtn.addEventListener('click', async () => { if (!installPrompt) return; installPrompt.prompt(); await installPrompt.userChoice; installPrompt = null; installBtn.hidden = true; });
if ('serviceWorker' in navigator && (location.protocol === 'https:' || location.hostname === 'localhost')) window.addEventListener('load', () => navigator.serviceWorker.register('/static/sw.js').catch(console.warn));
loadHistory();
