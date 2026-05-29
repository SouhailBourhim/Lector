/* ═══════════════════════════════════════════════════
   LECTOR — Frontend JavaScript (Phase 3)
   ═══════════════════════════════════════════════════ */

// ── State ──────────────────────────────────────────────────────────────────
let currentJobId    = null;
let currentChapters = [];   // [{number, title}]
let sseSource       = null;
let currentBookName = '';
let wavesurfers     = {};   // chapter_number → WaveSurfer instance
let jobComplete     = false;

// ── Screen management ──────────────────────────────────────────────────────
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

// ── Error banner ────────────────────────────────────────────────────────────
function showError(msg) {
  document.getElementById('error-text').textContent = msg;
  document.getElementById('error-banner').classList.remove('hidden');
}
function hideError() {
  document.getElementById('error-banner').classList.add('hidden');
}

// ── Upload status ────────────────────────────────────────────────────────────
function showUploadStatus(text) {
  document.getElementById('upload-status-text').textContent = text;
  document.getElementById('upload-status').classList.remove('hidden');
}
function hideUploadStatus() {
  document.getElementById('upload-status').classList.add('hidden');
}

// ── Skeleton loader ──────────────────────────────────────────────────────────
function showSkeletonChapters(count) {
  const list = document.getElementById('chapter-list');
  list.innerHTML = '';
  for (let i = 0; i < Math.min(count, 12); i++) {
    const row = document.createElement('div');
    row.className = 'skeleton-row';
    list.appendChild(row);
  }
}

// ── Upload ──────────────────────────────────────────────────────────────────
async function uploadFile(file) {
  hideError();
  const ext = file.name.split('.').pop().toLowerCase();
  if (!['pdf', 'epub'].includes(ext)) {
    showError('Only PDF and EPUB files are supported.');
    return;
  }

  // Show skeleton while uploading + parsing
  showScreen('app');
  showSkeletonChapters(8);
  document.getElementById('book-name-display').textContent = file.name.replace(/\.[^.]+$/, '');
  document.getElementById('chapter-count-display').textContent = 'Parsing…';
  document.getElementById('right-placeholder').style.display = 'flex';
  document.getElementById('progress-view').classList.add('hidden');
  document.getElementById('result-view').classList.add('hidden');
  document.getElementById('generate-btn').disabled = true;

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
    document.getElementById('generate-btn').disabled = false;

    hideUploadStatus();
  } catch (e) {
    hideUploadStatus();
    showError(e.message);
    // Revert to upload screen on failure
    showScreen('upload');
  }
}

// ── Voices ──────────────────────────────────────────────────────────────────
async function loadVoices() {
  const sel = document.getElementById('voice-select');
  sel.innerHTML = '<option value="">Loading voices…</option>';
  try {
    const resp   = await fetch('/voices');
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

// ── Chapter list ─────────────────────────────────────────────────────────────
function renderChapterList(chapters) {
  const list = document.getElementById('chapter-list');
  list.innerHTML = '';

  chapters.forEach(ch => {
    const row   = document.createElement('div');
    row.className = 'chapter-row';
    row.dataset.chapterNum = ch.number;

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
    title.textContent = ch.title;

    label.append(cb, num, title);
    row.appendChild(label);
    list.appendChild(row);
  });

  const selectAll = document.getElementById('select-all');
  selectAll.checked = true;
  selectAll.onchange = e => {
    document.querySelectorAll('.chapter-cb').forEach(cb => {
      cb.checked = e.target.checked;
    });
  };

  list.addEventListener('change', () => {
    const all     = document.querySelectorAll('.chapter-cb');
    const checked = document.querySelectorAll('.chapter-cb:checked');
    selectAll.indeterminate = checked.length > 0 && checked.length < all.length;
    selectAll.checked = checked.length === all.length;
  });
}

function markChapterDone(chapterNum) {
  const row = document.querySelector(`.chapter-row[data-chapter-num="${chapterNum}"]`);
  if (!row) return;
  row.classList.add('done');
  const checkSpan = document.createElement('span');
  checkSpan.className   = 'chapter-check';
  checkSpan.textContent = '✓';
  row.appendChild(checkSpan);
}

function backToUpload() {
  if (sseSource) { sseSource.close(); sseSource = null; }
  jobComplete = false;
  destroyAllWavesurfers();
  showScreen('upload');
}

// ── Synthesis ─────────────────────────────────────────────────────────────────
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

    // Show progress view, hide result + placeholder
    document.getElementById('right-placeholder').style.display = 'none';
    document.getElementById('result-view').classList.add('hidden');
    document.getElementById('progress-view').classList.remove('hidden');
    document.getElementById('audio-list').innerHTML = '';
    document.getElementById('activity-entries').innerHTML = '';
    destroyAllWavesurfers();

    setProgress(0, 'pending', 'Starting…');
    jobComplete = false;
    listenProgress();
  } catch (e) {
    btn.disabled = false;
    showError(e.message);
  }
}

// ── SSE progress ──────────────────────────────────────────────────────────────
function listenProgress() {
  if (sseSource) sseSource.close();
  sseSource = new EventSource(`/progress/${currentJobId}`);

  sseSource.onmessage = event => {
    let d;
    try { d = JSON.parse(event.data); }
    catch (_) { return; }

    if (d.status === 'chapter_ready') {
      // Append audio card immediately; keep progress view visible
      appendAudioCard(d.chapter);
      markChapterDone(d.chapter.number);
      setProgress(d.progress || 0, 'synthesizing', d.message || '');
      addActivity(d.message || `Chapter ${d.chapter.number} ready`);
      showResultView();  // show result area alongside progress
      return;
    }

    setProgress(d.progress || 0, d.status || '', d.message || '');

    if (d.message) addActivity(d.message);

    if (d.status === 'done') {
      jobComplete = true;
      sseSource.close();
      sseSource = null;

      // Render any chapters not yet shown via chapter_ready (e.g. after reconnect)
      if (d.chapters && d.chapters.length > 0) {
        const renderedNums = new Set(
          [...document.querySelectorAll('.audio-card')].map(c => parseInt(c.dataset.chapterNum))
        );
        d.chapters.forEach(ch => {
          if (!renderedNums.has(ch.number)) appendAudioCard(ch);
        });
      }

      document.getElementById('progress-view').classList.add('hidden');
      showResultView(d.chapters ? d.chapters.length : null);
      document.getElementById('generate-btn').disabled = false;
    } else if (d.status === 'error') {
      jobComplete = true;
      sseSource.close();
      sseSource = null;
      document.getElementById('progress-view').classList.add('hidden');
      document.getElementById('generate-btn').disabled = false;
      showError(d.error || 'Synthesis failed. Please try again.');
    }
  };

  sseSource.onerror = () => {
    if (sseSource) { sseSource.close(); sseSource = null; }
    if (jobComplete) return;
    setTimeout(() => {
      if (currentJobId && !jobComplete) listenProgress();
    }, 2000);
  };
}

// ── Progress ring + activity ──────────────────────────────────────────────────
const RING_CIRCUMFERENCE = 2 * Math.PI * 60; // 376.99

function setProgress(value, status, message) {
  const pct = Math.round((value || 0) * 100);
  const offset = RING_CIRCUMFERENCE * (1 - (value || 0));

  const fill = document.getElementById('ring-fill');
  if (fill) fill.style.strokeDashoffset = offset;

  const pctText = document.getElementById('ring-pct-text');
  if (pctText) pctText.textContent = `${pct}%`;

  const statusLabel = document.getElementById('ring-status-label');
  if (statusLabel) statusLabel.textContent = status || '';

  const pill = document.getElementById('status-pill');
  if (pill) pill.textContent = status || '';

  const sub = document.getElementById('progress-sub');
  if (sub && message) sub.textContent = message;

  const wrap = document.getElementById('progress-ring-wrap');
  if (wrap) wrap.setAttribute('aria-valuenow', pct);
}

function addActivity(msg) {
  if (!msg) return;
  const container = document.getElementById('activity-entries');
  if (!container) return;

  const entry = document.createElement('div');
  entry.className = 'activity-entry';

  const dot = document.createElement('span');
  dot.className = 'activity-dot';

  const text = document.createElement('span');
  text.textContent = msg;

  entry.append(dot, text);
  container.appendChild(entry);
  container.scrollTop = container.scrollHeight;

  // Keep at most 40 entries
  while (container.children.length > 40) {
    container.removeChild(container.firstChild);
  }
}

// ── Result view ───────────────────────────────────────────────────────────────
function showResultView(totalChapters) {
  const resultView = document.getElementById('result-view');
  resultView.classList.remove('hidden');

  const sub = document.getElementById('result-sub');
  const cards = document.querySelectorAll('.audio-card');
  const count = totalChapters ?? cards.length;
  if (count > 0) {
    sub.textContent = `${count} chapter${count !== 1 ? 's' : ''} · ready to play or download`;
  }
}

function appendAudioCard(ch) {
  const list = document.getElementById('audio-list');

  // Don't double-render
  if (document.querySelector(`.audio-card[data-chapter-num="${ch.number}"]`)) return;

  const chParam = ch.number != null ? `?chapter=${ch.number}` : '';
  const audioSrc     = `/audio/${currentJobId}${chParam}`;
  const downloadHref = `/download/${currentJobId}${chParam}`;

  const card = document.createElement('div');
  card.className        = 'audio-card';
  card.dataset.chapterNum = ch.number;

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
  chTitle.textContent = ch.title;
  header.appendChild(chTitle);

  // WaveSurfer container
  const waveWrap = document.createElement('div');
  waveWrap.className = 'waveform-wrap';
  const waveId = `wave-${ch.number}`;
  waveWrap.id = waveId;

  // Speed controls
  const controls = document.createElement('div');
  controls.className = 'player-controls';
  const speedLabel = document.createElement('span');
  speedLabel.className   = 'speed-label';
  speedLabel.textContent = 'Speed';
  controls.appendChild(speedLabel);

  [0.75, 1, 1.25, 1.5].forEach(rate => {
    const btn = document.createElement('button');
    btn.className   = `speed-btn${rate === 1 ? ' active' : ''}`;
    btn.textContent = `${rate}×`;
    btn.dataset.rate = rate;
    btn.onclick = () => {
      const ws = wavesurfers[ch.number];
      if (ws) ws.setPlaybackRate(rate, true);
      controls.querySelectorAll('.speed-btn').forEach(b => b.classList.remove('active'));
      btn.classList.add('active');
    };
    controls.appendChild(btn);
  });

  // Download link
  const actions = document.createElement('div');
  actions.className = 'audio-card-actions';
  const link = document.createElement('a');
  link.href       = downloadHref;
  link.className  = 'btn btn-secondary';
  link.download   = true;
  link.textContent = '⬇ Download MP3';
  actions.appendChild(link);

  card.append(header, waveWrap, controls, actions);
  list.appendChild(card);

  // Init WaveSurfer after card is in the DOM
  requestAnimationFrame(() => initWaveSurfer(waveId, ch.number, audioSrc));
}

// ── WaveSurfer ────────────────────────────────────────────────────────────────
function initWaveSurfer(containerId, chapterNum, audioSrc) {
  if (!window.WaveSurfer) {
    // Fallback: native audio player
    const container = document.getElementById(containerId);
    if (!container) return;
    const audio = document.createElement('audio');
    audio.controls  = true;
    audio.preload   = 'none';
    audio.src       = audioSrc;
    audio.className = 'audio-player';
    audio.setAttribute('aria-label', `Audio for chapter ${chapterNum}`);
    container.replaceWith(audio);
    return;
  }

  const container = document.getElementById(containerId);
  if (!container) return;

  try {
    const ws = WaveSurfer.create({
      container,
      waveColor:     '#3f3f46',
      progressColor: '#f59e0b',
      cursorColor:   '#f59e0b',
      barWidth:      2,
      barGap:        1,
      barRadius:     2,
      height:        56,
      normalize:     true,
      backend:       'WebAudio',
      url:           audioSrc,
    });
    wavesurfers[chapterNum] = ws;
  } catch (_) {
    // WaveSurfer failed — render native audio as fallback
    const audio = document.createElement('audio');
    audio.controls  = true;
    audio.preload   = 'none';
    audio.src       = audioSrc;
    audio.className = 'audio-player';
    container.replaceWith(audio);
  }
}

function destroyAllWavesurfers() {
  Object.values(wavesurfers).forEach(ws => { try { ws.destroy(); } catch (_) {} });
  wavesurfers = {};
}

// ── Convert another ───────────────────────────────────────────────────────────
function convertAnother() {
  if (sseSource) { sseSource.close(); sseSource = null; }
  jobComplete = false;
  destroyAllWavesurfers();
  currentJobId    = null;
  currentChapters = [];
  currentBookName = '';
  document.getElementById('audio-list').innerHTML = '';
  document.getElementById('generate-btn').disabled = false;
  const fi = document.getElementById('file-input');
  if (fi) fi.value = '';
  showScreen('upload');
}

// ── Drag & drop + file input ─────────────────────────────────────────────────
document.addEventListener('DOMContentLoaded', () => {
  const dropZone  = document.getElementById('drop-zone');
  const fileInput = document.getElementById('file-input');

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
