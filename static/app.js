/* ═══════════════════════════════════════════════════
   LECTOR — Frontend JavaScript
   ═══════════════════════════════════════════════════ */

// ── State ──────────────────────────────────────────────────────────────────
let currentJobId   = null;
let currentChapters = [];   // [{number, title}]
let sseSource      = null;
let currentBookName = '';

// ── Utilities ──────────────────────────────────────────────────────────────
function escapeHtml(s) {
  return String(s)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;');
}

function showScreen(name) {
  document.querySelectorAll('.screen').forEach(s => {
    s.classList.remove('active');
    s.classList.add('hidden');
  });
  const el = document.getElementById(`screen-${name}`);
  if (el) {
    el.classList.remove('hidden');
    el.classList.add('active');
  }
}

function showError(msg) {
  const banner = document.getElementById('error-banner');
  document.getElementById('error-text').textContent = msg;
  banner.classList.remove('hidden');
}
function hideError() {
  document.getElementById('error-banner').classList.add('hidden');
}

function showUploadStatus(text) {
  document.getElementById('upload-status-text').textContent = text;
  document.getElementById('upload-status').classList.remove('hidden');
}
function hideUploadStatus() {
  document.getElementById('upload-status').classList.add('hidden');
}

// ── Upload ─────────────────────────────────────────────────────────────────
async function uploadFile(file) {
  hideError();
  const ext = file.name.split('.').pop().toLowerCase();
  if (!['pdf', 'epub'].includes(ext)) {
    showError('Only PDF and EPUB files are supported.');
    return;
  }

  showUploadStatus(`Uploading ${file.name}…`);

  const formData = new FormData();
  formData.append('file', file);

  try {
    const resp = await fetch('/upload', { method: 'POST', body: formData });
    const data = await resp.json();
    if (!resp.ok) throw new Error(data.detail || 'Upload failed.');

    currentJobId     = data.job_id;
    currentChapters  = data.chapters;
    currentBookName  = file.name.replace(/\.[^.]+$/, '');

    await loadVoices();
    renderChapterList(data.chapters);

    document.getElementById('book-name-display').textContent = currentBookName;
    document.getElementById('chapter-count-display').textContent =
      `${data.chapters.length} chapter${data.chapters.length !== 1 ? 's' : ''}`;

    hideUploadStatus();
    showScreen('chapters');
  } catch (e) {
    hideUploadStatus();
    showError(e.message);
  }
}

// ── Voices ─────────────────────────────────────────────────────────────────
async function loadVoices() {
  const sel = document.getElementById('voice-select');
  sel.innerHTML = '<option value="">Loading voices…</option>';
  try {
    const resp  = await fetch('/voices');
    const voices = await resp.json();
    sel.innerHTML = '';
    voices.forEach(v => {
      const opt = document.createElement('option');
      opt.value = v.name;
      opt.textContent = `${v.display} · ${v.locale} · ${v.gender}`;
      if (v.name === 'en-US-AriaNeural') opt.selected = true;
      sel.appendChild(opt);
    });
  } catch (_) {
    sel.innerHTML = '<option value="en-US-AriaNeural">Aria · en-US · Female</option>';
  }
}

// ── Chapter list ────────────────────────────────────────────────────────────
function renderChapterList(chapters) {
  const list = document.getElementById('chapter-list');
  list.innerHTML = '';

  chapters.forEach(ch => {
    const row   = document.createElement('div');
    row.className = 'chapter-row';

    const label = document.createElement('label');
    label.className = 'chapter-label';

    const cb = document.createElement('input');
    cb.type      = 'checkbox';
    cb.className = 'chapter-cb';
    cb.value     = ch.number;
    cb.checked   = true;

    const num = document.createElement('span');
    num.className   = 'chapter-num';
    num.textContent = ch.number;

    const title = document.createElement('span');
    title.className   = 'chapter-title';
    title.textContent = ch.title;   // textContent — no XSS risk

    label.append(cb, num, title);
    row.appendChild(label);
    list.appendChild(row);
  });

  // Select-all toggle
  const selectAll = document.getElementById('select-all');
  selectAll.checked = true;
  selectAll.onchange = e => {
    document.querySelectorAll('.chapter-cb').forEach(cb => {
      cb.checked = e.target.checked;
    });
  };

  // Individual checkbox → update select-all state
  list.addEventListener('change', () => {
    const all  = document.querySelectorAll('.chapter-cb');
    const checked = document.querySelectorAll('.chapter-cb:checked');
    selectAll.indeterminate = checked.length > 0 && checked.length < all.length;
    selectAll.checked = checked.length === all.length;
  });
}

function backToUpload() {
  if (sseSource) { sseSource.close(); sseSource = null; }
  showScreen('upload');
}

// ── Synthesis ──────────────────────────────────────────────────────────────
async function startSynthesis() {
  hideError();
  const selected = [...document.querySelectorAll('.chapter-cb:checked')]
    .map(cb => parseInt(cb.value, 10));

  if (selected.length === 0) {
    showError('Please select at least one chapter.');
    return;
  }

  const voice = document.getElementById('voice-select').value || 'en-US-AriaNeural';
  const btn   = document.getElementById('generate-btn');
  btn.disabled = true;

  try {
    const resp = await fetch(`/synthesize/${currentJobId}`, {
      method:  'POST',
      headers: { 'Content-Type': 'application/json' },
      body:    JSON.stringify({ chapters: selected, voice }),
    });
    const data = await resp.json();
    if (!resp.ok) throw new Error(data.detail || 'Could not start synthesis.');

    // Reset progress UI
    setProgress(0, 'Starting…', 'pending');
    showScreen('progress');
    listenProgress();
  } catch (e) {
    btn.disabled = false;
    showError(e.message);
  }
}

// ── SSE progress ────────────────────────────────────────────────────────────
function listenProgress() {
  if (sseSource) sseSource.close();
  sseSource = new EventSource(`/progress/${currentJobId}`);

  sseSource.onmessage = event => {
    let d;
    try { d = JSON.parse(event.data); }
    catch (_) { return; }

    setProgress(d.progress || 0, d.message || '', d.status || '');

    if (d.status === 'done') {
      sseSource.close();
      sseSource = null;
      renderResult(d.chapters || [], d.total_duration_s);
      showScreen('result');
    } else if (d.status === 'error') {
      sseSource.close();
      sseSource = null;
      showScreen('chapters');
      document.getElementById('generate-btn').disabled = false;
      showError(d.error || 'Synthesis failed. Please try again.');
    }
  };

  sseSource.onerror = () => {
    // Auto-reconnect if the job isn't done
    sseSource.close();
    setTimeout(() => {
      if (currentJobId) listenProgress();
    }, 2000);
  };
}

function setProgress(value, message, status) {
  const pct = Math.round((value || 0) * 100);
  document.getElementById('progress-fill').style.width = `${pct}%`;
  document.getElementById('progress-pct').textContent  = `${pct}%`;
  document.getElementById('progress-message').textContent = message || '';
  document.getElementById('status-pill').textContent = status || '';
  const track = document.getElementById('progress-track');
  if (track) track.setAttribute('aria-valuenow', pct);
}

// ── Result screen ────────────────────────────────────────────────────────────
function renderResult(chapters, totalDurationS) {
  const list = document.getElementById('audio-list');
  list.innerHTML = '';

  // Summary line
  const sub = document.getElementById('result-sub');
  if (chapters.length > 0) {
    sub.textContent = `${chapters.length} chapter${chapters.length !== 1 ? 's' : ''} · ready to play or download`;
  }

  if (chapters.length === 0) {
    // Fallback: single audio card with no chapter param
    chapters = [{ number: null, title: currentBookName || 'Audiobook' }];
  }

  chapters.forEach(ch => {
    const chParam = ch.number != null ? `?chapter=${ch.number}` : '';
    const card = document.createElement('div');
    card.className = 'audio-card';

    // Header
    const header = document.createElement('div');
    header.className = 'audio-card-header';
    if (ch.number != null) {
      const badge = document.createElement('span');
      badge.className   = 'ch-badge';
      badge.textContent = ch.number;
      header.appendChild(badge);
    }
    const chTitle = document.createElement('span');
    chTitle.className   = 'ch-title';
    chTitle.textContent = ch.title;   // textContent — no XSS risk
    header.appendChild(chTitle);

    // Audio player — src is /audio/{uuid}?chapter=N, fully server-controlled
    const audio = document.createElement('audio');
    audio.controls  = true;
    audio.preload   = 'none';
    audio.src       = `/audio/${currentJobId}${chParam}`;
    audio.className = 'audio-player';
    audio.setAttribute('aria-label', `Audio for chapter ${ch.number ?? ''}`);

    // Download link
    const actions = document.createElement('div');
    actions.className = 'audio-card-actions';
    const link = document.createElement('a');
    link.href       = `/download/${currentJobId}${chParam}`;
    link.className  = 'btn btn-secondary btn-download';
    link.download   = true;
    link.textContent = '⬇ Download MP3';
    actions.appendChild(link);

    card.append(header, audio, actions);
    list.appendChild(card);
  });
}

function convertAnother() {
  if (sseSource) { sseSource.close(); sseSource = null; }
  currentJobId    = null;
  currentChapters = [];
  currentBookName = '';
  document.getElementById('audio-list').innerHTML = '';
  document.getElementById('generate-btn').disabled = false;
  // Reset file input
  const fi = document.getElementById('file-input');
  if (fi) fi.value = '';
  showScreen('upload');
}

// ── Drag & drop + file input ────────────────────────────────────────────────
document.addEventListener('DOMContentLoaded', () => {
  const dropZone  = document.getElementById('drop-zone');
  const fileInput = document.getElementById('file-input');

  // Drag over entire window → highlight drop zone
  window.addEventListener('dragover', e => e.preventDefault());
  window.addEventListener('drop',     e => e.preventDefault());

  dropZone.addEventListener('dragenter', e => {
    e.preventDefault();
    dropZone.classList.add('drag-over');
  });
  dropZone.addEventListener('dragover', e => {
    e.preventDefault();
    dropZone.classList.add('drag-over');
  });
  dropZone.addEventListener('dragleave', e => {
    if (!dropZone.contains(e.relatedTarget))
      dropZone.classList.remove('drag-over');
  });
  dropZone.addEventListener('drop', e => {
    e.preventDefault();
    dropZone.classList.remove('drag-over');
    const file = e.dataTransfer?.files?.[0];
    if (file) uploadFile(file);
  });

  // Keyboard activate for accessibility
  dropZone.addEventListener('keydown', e => {
    if (e.key === 'Enter' || e.key === ' ') {
      e.preventDefault();
      fileInput.click();
    }
  });

  fileInput.addEventListener('change', () => {
    if (fileInput.files?.[0]) uploadFile(fileInput.files[0]);
  });
});
