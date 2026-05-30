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

// ── Mini player ─────────────────────────────────────────────────────────────
// Stays alive across screen changes. When WaveSurfer is destroyed (e.g. on
// "Convert another"), audio is handed off to a standalone <audio> element so
// playback never interrupts unexpectedly.
const mp = {
  ws:     null,   // active WaveSurfer (null when using fallback audio)
  audio:  null,   // standalone <audio> used after WS is destroyed
  raf:    null,   // requestAnimationFrame id for scrub updates

  // ── read-only state ───────────────────────────────────────────────────────
  get isPlaying() {
    if (this.ws)    return this.ws.isPlaying();
    if (this.audio) return !this.audio.paused;
    return false;
  },
  get currentTime() {
    if (this.ws)    return this.ws.getCurrentTime();
    if (this.audio) return this.audio.currentTime;
    return 0;
  },
  get duration() {
    if (this.ws)    return this.ws.getDuration() || 0;
    if (this.audio) return this.audio.duration || 0;
    return 0;
  },

  // ── activate from a WaveSurfer instance ───────────────────────────────────
  activate(ws, title, book) {
    // Stop any existing standalone audio
    if (this.audio) { this.audio.pause(); this.audio = null; }
    this.ws = ws;
    this._updateMeta(title, book);
    this._show();
    this._syncPlayBtn(ws.isPlaying());
    this._startRaf();

    ws.on('play',   () => { this._syncPlayBtn(true);  this._startRaf(); });
    ws.on('pause',  () => this._syncPlayBtn(false));
    ws.on('finish', () => { this._syncPlayBtn(false); this._setFill(0); });
  },

  // ── hand off to standalone <audio> before WS is destroyed ─────────────────
  // Call this in convertAnother()/backToUpload() BEFORE destroyAllWavesurfers().
  ejectWaveSurfer() {
    if (!this.ws) return;
    const wasPlaying = this.ws.isPlaying();
    const curTime    = this.ws.getCurrentTime();
    let   src;
    try { src = this.ws.getMediaElement().src; } catch (_) { src = null; }

    this.ws = null;
    cancelAnimationFrame(this.raf);

    if (!src) { this._hide(); return; }   // can't recover without src

    this.audio = new Audio(src);
    this.audio.currentTime = curTime;
    if (wasPlaying) this.audio.play().catch(() => {});
    this._syncPlayBtn(wasPlaying);

    this.audio.addEventListener('play',       () => { this._syncPlayBtn(true);  this._startRaf(); });
    this.audio.addEventListener('pause',      () => this._syncPlayBtn(false));
    this.audio.addEventListener('ended',      () => { this._syncPlayBtn(false); this._setFill(0); });
    this.audio.addEventListener('timeupdate', () => this._tick());
    this._startRaf();
  },

  // ── controls ──────────────────────────────────────────────────────────────
  playPause() {
    if (this.ws) { this.ws.playPause(); return; }
    if (this.audio) this.audio.paused ? this.audio.play() : this.audio.pause();
  },

  seekTo(fraction) {
    const f = Math.max(0, Math.min(1, fraction));
    if (this.ws) { this.ws.seekTo(f); return; }
    if (this.audio && isFinite(this.audio.duration)) {
      this.audio.currentTime = f * this.audio.duration;
    }
  },

  // ── internal ──────────────────────────────────────────────────────────────
  _show() {
    document.getElementById('mini-player').classList.remove('mini-player--hidden');
  },
  _hide() {
    document.getElementById('mini-player').classList.add('mini-player--hidden');
    this.ws = null; this.audio = null;
    cancelAnimationFrame(this.raf);
  },
  _updateMeta(title, book) {
    document.getElementById('mp-title').textContent = title || '—';
    document.getElementById('mp-book').textContent  = book  || '';
  },
  _syncPlayBtn(playing) {
    document.getElementById('mp-play').classList.toggle('playing', playing);
  },
  _setFill(pct) {
    document.getElementById('mp-fill').style.width = pct + '%';
  },
  _tick() {
    const dur = this.duration;
    const cur = this.currentTime;
    this._setFill(dur > 0 ? (cur / dur * 100) : 0);
    document.getElementById('mp-time').textContent = fmtTime(cur);
    document.getElementById('mp-dur').textContent  = isFinite(dur) && dur > 0 ? fmtTime(dur) : '--:--';
  },
  _startRaf() {
    cancelAnimationFrame(this.raf);
    const tick = () => { this._tick(); if (this.isPlaying) this.raf = requestAnimationFrame(tick); };
    this.raf = requestAnimationFrame(tick);
  },
};

// Wire mini-player controls once DOM is ready (called from DOMContentLoaded)
function initMiniPlayer() {
  document.getElementById('mp-play').addEventListener('click', () => mp.playPause());
  document.getElementById('mp-close').addEventListener('click', () => {
    if (mp.ws)    mp.ws.pause();
    if (mp.audio) mp.audio.pause();
    mp._hide();
  });

  const bar = document.getElementById('mp-bar');
  const seek = e => {
    const r = bar.getBoundingClientRect();
    mp.seekTo((e.clientX - r.left) / r.width);
  };
  bar.addEventListener('click', seek);

  // Keyboard seek on scrub bar (left/right arrow ±5 s)
  bar.addEventListener('keydown', e => {
    const dur = mp.duration;
    if (!dur) return;
    if (e.key === 'ArrowLeft')  { e.preventDefault(); mp.seekTo(Math.max(0, mp.currentTime - 5) / dur); }
    if (e.key === 'ArrowRight') { e.preventDefault(); mp.seekTo(Math.min(dur, mp.currentTime + 5) / dur); }
  });
}

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

    // Persist so the session survives a page reload
    localStorage.setItem('lector_job_id',    currentJobId);
    localStorage.setItem('lector_book_name', currentBookName);

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
  mp.ejectWaveSurfer();   // hand off to standalone <audio> before WS is destroyed
  destroyAllWavesurfers();
  localStorage.removeItem('lector_job_id');
  localStorage.removeItem('lector_book_name');
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

    if (d.status === 'preview_ready') {
      // Show a preview audio card so the user can start listening immediately.
      // When the full chapter arrives via chapter_ready, the card's source
      // is swapped in place (WaveSurfer reloads the new URL).
      appendAudioCard(d.chapter, /* isPreview */ true);
      markChapterDone(d.chapter.number);
      setProgress(d.progress || 0, 'synthesizing', `Preview ready — ${d.chapter.title}`);
      addActivity(`Preview ready — ${d.chapter.title}`);
      showResultView();
      return;
    }

    if (d.status === 'chapter_ready') {
      // Full chapter done — append new card or upgrade existing preview card.
      const existing = document.querySelector(`.audio-card[data-chapter-num="${d.chapter.number}"]`);
      if (existing) {
        // Swap the WaveSurfer source to the full chapter
        const ws = wavesurfers[d.chapter.number];
        if (ws) {
          ws.load(`/audio/${currentJobId}?chapter=${d.chapter.number}`);
        }
        const badge = existing.querySelector('.ch-preview-badge');
        if (badge) badge.remove();
      } else {
        appendAudioCard(d.chapter, /* isPreview */ false);
      }
      markChapterDone(d.chapter.number);
      setProgress(d.progress || 0, 'synthesizing', d.message || '');
      addActivity(d.message || `Chapter ${d.chapter.number} ready`);
      showResultView();
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

function appendAudioCard(ch, isPreview = false) {
  const list = document.getElementById('audio-list');

  // Don't double-render (preview_ready and chapter_ready both call this)
  if (document.querySelector(`.audio-card[data-chapter-num="${ch.number}"]`)) return;

  const chParam      = ch.number != null ? `?chapter=${ch.number}` : '';
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

  if (isPreview) {
    const previewBadge = document.createElement('span');
    previewBadge.className   = 'ch-preview-badge';
    previewBadge.textContent = 'PREVIEW';
    previewBadge.title       = 'Full chapter is still synthesizing…';
    header.appendChild(previewBadge);
  }

  // Play / pause button (right-aligned in header)
  const playBtn = document.createElement('button');
  playBtn.className = 'card-play-btn';
  playBtn.setAttribute('aria-label', 'Play');
  playBtn.innerHTML =
    '<svg class="ico-play"  viewBox="0 0 24 24" fill="currentColor"><path d="M8 5v14l11-7z"/></svg>' +
    '<svg class="ico-pause" viewBox="0 0 24 24" fill="currentColor"><path d="M6 5h4v14H6zM14 5h4v14h-4z"/></svg>';
  header.appendChild(playBtn);

  // Time display
  const timeLine = document.createElement('div');
  timeLine.className = 'card-time-line';
  const timeEl  = document.createElement('span');
  timeEl.className   = 'card-current-time';
  timeEl.textContent = '0:00';
  const durEl   = document.createElement('span');
  durEl.className   = 'card-duration';
  durEl.textContent = '--:--';
  timeLine.append(timeEl, durEl);

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

  card.append(header, waveWrap, timeLine, controls, actions);
  list.appendChild(card);

  // Init WaveSurfer after card is in the DOM
  requestAnimationFrame(() => initWaveSurfer(waveId, ch.number, audioSrc, playBtn, timeEl, durEl));
}

// ── WaveSurfer ────────────────────────────────────────────────────────────────
function fmtTime(s) {
  if (!isFinite(s) || s < 0) return '0:00';
  const m = Math.floor(s / 60), sec = Math.floor(s % 60);
  return `${m}:${String(sec).padStart(2, '0')}`;
}

function initWaveSurfer(containerId, chapterNum, audioSrc, playBtn, timeEl, durEl) {
  const container = document.getElementById(containerId);
  if (!container) return;

  if (!window.WaveSurfer) {
    // Fallback: native audio player
    const audio = document.createElement('audio');
    audio.controls  = true;
    audio.preload   = 'none';
    audio.src       = audioSrc;
    audio.className = 'audio-player';
    audio.setAttribute('aria-label', `Audio for chapter ${chapterNum}`);
    if (playBtn) playBtn.remove();
    container.replaceWith(audio);
    return;
  }

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

    // Wire play button
    if (playBtn) {
      playBtn.onclick = () => ws.playPause();
      ws.on('play',   () => playBtn.classList.add('playing'));
      ws.on('pause',  () => playBtn.classList.remove('playing'));
      ws.on('finish', () => playBtn.classList.remove('playing'));
    }

    // Wire time display
    ws.on('ready', dur => {
      if (durEl) durEl.textContent = fmtTime(dur);
    });
    ws.on('timeupdate', cur => {
      if (timeEl) timeEl.textContent = fmtTime(cur);
    });
    ws.on('finish', () => {
      if (timeEl && durEl) timeEl.textContent = '0:00';
    });

    // Activate the persistent mini-player whenever this chapter starts playing
    ws.on('play', () => {
      // Find chapter title from the card header
      const card  = document.querySelector(`.audio-card[data-chapter-num="${chapterNum}"]`);
      const title = card ? (card.querySelector('.ch-title')?.textContent || `Chapter ${chapterNum}`) : `Chapter ${chapterNum}`;
      mp.activate(ws, title, currentBookName);
    });

  } catch (_) {
    // WaveSurfer failed — render native audio as fallback
    const audio = document.createElement('audio');
    audio.controls  = true;
    audio.preload   = 'none';
    audio.src       = audioSrc;
    audio.className = 'audio-player';
    if (playBtn) playBtn.remove();
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
  mp.ejectWaveSurfer();   // hand off to standalone <audio> before WS is destroyed
  destroyAllWavesurfers();
  currentJobId    = null;
  currentChapters = [];
  currentBookName = '';
  localStorage.removeItem('lector_job_id');
  localStorage.removeItem('lector_book_name');
  document.getElementById('audio-list').innerHTML = '';
  document.getElementById('generate-btn').disabled = false;
  const fi = document.getElementById('file-input');
  if (fi) fi.value = '';
  showScreen('upload');
}

// ── Session restore (page reload) ────────────────────────────────────────────
async function restoreSession(jobId) {
  const resp = await fetch(`/job/${jobId}`);
  if (!resp.ok) throw new Error('Job not found');
  const job = await resp.json();

  currentJobId    = job.id;
  currentChapters = job.chapters;
  currentBookName = localStorage.getItem('lector_book_name') || 'Your book';

  // Populate sidebar
  showScreen('app');
  await loadVoices();
  renderChapterList(job.chapters);
  document.getElementById('book-name-display').textContent = currentBookName;
  document.getElementById('chapter-count-display').textContent =
    `${job.chapters.length} chapter${job.chapters.length !== 1 ? 's' : ''}`;

  // Mark already-selected chapters checked
  if (job.selected && job.selected.length) {
    document.querySelectorAll('.chapter-cb').forEach(cb => {
      cb.checked = job.selected.includes(parseInt(cb.value, 10));
    });
  }

  const terminal = ['done', 'error'];
  const active   = ['queued', 'analyzing', 'synthesizing', 'assembling'];

  if (job.status === 'done' || job.audio_ready.length > 0 || job.preview_ready.length > 0) {
    // Show result view and rebuild audio cards
    document.getElementById('right-placeholder').style.display = 'none';
    document.getElementById('progress-view').classList.add('hidden');

    const allChapters = [...job.audio_ready, ...job.preview_ready];
    allChapters.sort((a, b) => a.number - b.number);
    const audioNums = new Set(job.audio_ready.map(c => c.number));

    allChapters.forEach(ch => {
      appendAudioCard(ch, !audioNums.has(ch.number));
      markChapterDone(ch.number);
    });

    showResultView(job.audio_ready.length || null);
    document.getElementById('generate-btn').disabled = false;

    if (active.includes(job.status)) {
      // Still synthesizing — reconnect SSE to pick up remaining chapters
      jobComplete = false;
      document.getElementById('progress-view').classList.remove('hidden');
      setProgress(job.progress || 0, job.status, job.message || '');
      listenProgress();
    } else {
      jobComplete = true;
    }
  } else if (active.includes(job.status)) {
    // No audio yet but job is running — show progress and reconnect
    jobComplete = false;
    document.getElementById('right-placeholder').style.display = 'none';
    document.getElementById('progress-view').classList.remove('hidden');
    setProgress(job.progress || 0, job.status, job.message || '');
    listenProgress();
  } else if (job.status === 'error') {
    showError(job.error || 'Synthesis failed.');
    document.getElementById('generate-btn').disabled = false;
  } else if (job.status === 'pending') {
    // Uploaded but synthesis not started — stay on chapter-select screen
    document.getElementById('generate-btn').disabled = false;
  }
}

// ── Drag & drop + file input ─────────────────────────────────────────────────
document.addEventListener('DOMContentLoaded', () => {
  initMiniPlayer();
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

  // ── Restore session after page reload ───────────────────────────────────
  const savedJobId = localStorage.getItem('lector_job_id');
  if (savedJobId) {
    restoreSession(savedJobId).catch(() => {
      // Job no longer exists or server is down — clear stale state
      localStorage.removeItem('lector_job_id');
      localStorage.removeItem('lector_book_name');
    });
  }
});
