"""
Lector v2 — FastAPI Web Server
Run: uvicorn server:app --port 8000

Architecture:
  - All job state stored in SQLite (storage/db.py) — survives restarts
  - Bounded worker pool (workers/pool.py) — no unbounded create_task
  - SSE progress polled from DB — survives client reconnects
  - All files via StorageBackend — ready for S3 swap
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import aiofiles
import edge_tts
from fastapi import FastAPI, File, HTTPException, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel

# ─── Path bootstrap ───────────────────────────────────────────────────────────
BASE_DIR = Path(__file__).parent
sys.path.insert(0, str(BASE_DIR))

from config import DEFAULT_VOICE, VOICES  # noqa: E402

# ─── Config from env ──────────────────────────────────────────────────────────
PORT            = int(os.getenv("PORT", "8000"))
MAX_UPLOAD_MB   = int(os.getenv("MAX_UPLOAD_MB", "50"))
DATA_DIR        = Path(os.getenv("DATA_DIR", os.getenv("CACHE_DIR", "/tmp/lector_data")))
N_WORKERS       = int(os.getenv("N_WORKERS", "4"))
MAX_JOBS_PER_IP = int(os.getenv("MAX_JOBS_PER_IP", "3"))
MAX_GLOBAL_JOBS = int(os.getenv("MAX_GLOBAL_JOBS", "10"))
API_KEY         = os.getenv("API_KEY", "")          # empty = auth disabled
STATIC_VER      = "5"

# CPU-bound executor (spaCy, pydub)
_CPU_WORKERS  = max(4, (os.cpu_count() or 4) * 2)
_cpu_executor = ThreadPoolExecutor(max_workers=_CPU_WORKERS, thread_name_prefix="lector-cpu")

# ─── Logging ──────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger("lector")

# ─── App setup ────────────────────────────────────────────────────────────────
app = FastAPI(title="Lector", version="2.0.0")
app.add_middleware(
    CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"],
)
app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))

# ─── Global singletons (set at startup) ──────────────────────────────────────
repo:    "JobRepo | None"    = None   # type: ignore[assignment]
storage: "LocalStorage | None" = None  # type: ignore[assignment]
pool:    "WorkerPool | None" = None   # type: ignore[assignment]

# ─── Demo audio quotes ────────────────────────────────────────────────────────
DEMO_QUOTES: dict[str, str] = {
    "en-US-AriaNeural":    "Call me Ishmael. Some years ago, never mind how long precisely, having little money in my pocket and nothing particular to interest me on shore, I thought I would sail about a little.",
    "en-US-GuyNeural":     "It was a bright cold day in April, and the clocks were striking thirteen. Winston Smith, his chin nuzzled into his breast in an effort to escape the vile wind, slipped quickly through the glass doors.",
    "en-GB-SoniaNeural":   "It was the best of times, it was the worst of times, it was the age of wisdom, it was the age of foolishness, it was the epoch of belief, it was the epoch of incredulity.",
    "en-GB-RyanNeural":    "It is a truth universally acknowledged, that a single man in possession of a good fortune, must be in want of a wife. However little known the feelings or views of such a man may be on his first entering a neighbourhood.",
    "en-AU-NatashaNeural": "The sky above the port was the colour of television, tuned to a dead channel. All this happened, more or less.",
    "en-AU-WilliamNeural": "All happy families are alike; each unhappy family is unhappy in its own way. Everything was in confusion in the Oblonskys' house.",
    "en-CA-ClaraNeural":   "In a hole in the ground there lived a hobbit. Not a nasty, dirty, wet hole, filled with the ends of worms and an oozy smell, nor yet a dry, bare, sandy hole with nothing in it to sit down on or to eat.",
    "en-IN-NeerjaNeural":  "The past is a foreign country; they do things differently there. When I came upon the diary it was in the spring cleaning, and I thought no more of it than that it was old.",
}
_HERO_DEMO_TEXT = (
    "Mathematics, rightly viewed, possesses not only truth, but supreme beauty — "
    "a beauty cold and austere, like that of sculpture. "
    "The true spirit of delight, the exaltation, the sense of being more than human, "
    "which is the touchstone of the highest excellence, "
    "is to be found in mathematics as surely as in poetry. "
    "What is best in mathematics deserves not merely to be learned as a task, "
    "but to be assimilated as a part of daily thought, "
    "and brought again and again before the mind with ever-renewed encouragement."
)

# ─── Startup / shutdown ───────────────────────────────────────────────────────

@app.on_event("startup")
async def startup() -> None:
    global repo, storage, pool

    from storage.backend import LocalStorage
    from storage.db import JobRepo
    from workers.pool import WorkerPool

    DATA_DIR.mkdir(parents=True, exist_ok=True)

    storage = LocalStorage(root=DATA_DIR / "audio")
    repo    = JobRepo(db_path=DATA_DIR / "jobs.db")
    await repo.open()

    pool = WorkerPool(n_workers=N_WORKERS, repo=repo, storage=storage,
                      cpu_executor=_cpu_executor)
    await pool.start()

    asyncio.create_task(_cleanup_loop(), name="cleanup")
    log.info("Lector v2 started — data=%s  workers=%d  cpu_pool=%d",
             DATA_DIR, N_WORKERS, _CPU_WORKERS)


@app.on_event("shutdown")
async def shutdown() -> None:
    if pool:
        await pool.stop()
    if repo:
        await repo.close()
    _cpu_executor.shutdown(wait=False)


async def _cleanup_loop() -> None:
    """Delete jobs and files older than 1 hour; runs every 30 min."""
    while True:
        await asyncio.sleep(30 * 60)
        try:
            stale = await repo.list_stale(older_than_secs=3600)
            for job in stale:
                # Delete all audio files for this job
                audio_keys   = json.loads(job.audio_keys)
                preview_keys = json.loads(job.preview_keys)
                all_keys     = list(audio_keys.values()) + list(preview_keys.values())
                if job.book_key:
                    all_keys.append(job.book_key)
                for key in all_keys:
                    await storage.delete(key)
                await repo.delete(job.id)
                log.info("Cleaned up stale job %s", job.id)
        except Exception as exc:
            log.warning("Cleanup error: %s", exc)


# ─── Auth helper ──────────────────────────────────────────────────────────────

def _check_auth(request: Request) -> None:
    if not API_KEY:
        return
    auth = request.headers.get("Authorization", "")
    if auth != f"Bearer {API_KEY}":
        raise HTTPException(status_code=401, detail="Invalid or missing API key.")


def _client_ip(request: Request) -> str:
    fwd = request.headers.get("x-forwarded-for", "")
    return fwd.split(",")[0].strip() if fwd else (
        request.client.host if request.client else "unknown"
    )


def _safe_filename(s: str) -> str:
    return "".join(c if c.isalnum() or c in " -_()" else "_" for c in s).strip("_ ")[:80]


# ─── Health & Metrics ─────────────────────────────────────────────────────────

@app.get("/health")
async def health():
    return {
        "status":         "ok",
        "version":        "2.0.0",
        "queue_depth":    await repo.queue_depth(),
        "active_workers": pool.active_count if pool else 0,
        "cpu_workers":    _CPU_WORKERS,
    }


@app.get("/metrics")
async def metrics():
    c = pool.counters if pool else {}
    lines = [
        f'lector_jobs_done_total {c.get("jobs_done", 0)}',
        f'lector_jobs_error_total {c.get("jobs_error", 0)}',
        f'lector_cache_hits_total {c.get("cache_hits", 0)}',
        f'lector_tts_segments_total {c.get("tts_segments", 0)}',
        f'lector_active_workers {pool.active_count if pool else 0}',
        f'lector_queue_depth {await repo.queue_depth()}',
    ]
    return StreamingResponse(
        iter(["\n".join(lines) + "\n"]),
        media_type="text/plain; version=0.0.4",
    )


# ─── Pages ────────────────────────────────────────────────────────────────────

@app.get("/")
async def landing(request: Request):
    return templates.TemplateResponse(request=request, name="landing.html")


@app.get("/app")
async def index(request: Request):
    return templates.TemplateResponse(request=request, name="index.html",
                                      context={"static_ver": STATIC_VER})


# ─── Upload ───────────────────────────────────────────────────────────────────

@app.post("/upload")
async def upload_book(request: Request, file: UploadFile = File(...)):
    _check_auth(request)

    content_length = request.headers.get("content-length")
    if content_length and int(content_length) > MAX_UPLOAD_MB * 1024 * 1024:
        raise HTTPException(status_code=413, detail=f"File exceeds {MAX_UPLOAD_MB} MB.")

    ext = Path(file.filename or "").suffix.lower()
    if ext not in (".pdf", ".epub"):
        raise HTTPException(status_code=400, detail="Only PDF and EPUB files are supported.")

    from storage.db import Job, serialize_chapters

    job_id   = Job.new_id()
    book_key = f"books/{job_id}{ext}"

    # Stream to a temp file in 64 KB chunks (UploadFile.read() is the correct
    # async API — UploadFile is NOT an async iterable, so 'async for' raises).
    total     = 0
    limit     = MAX_UPLOAD_MB * 1024 * 1024
    chunk_sz  = 64 * 1024
    tmp_fd, tmp_name = tempfile.mkstemp(suffix=ext)
    tmp_path  = Path(tmp_name)
    try:
        with os.fdopen(tmp_fd, "wb") as tmp_fp:
            while True:
                chunk = await file.read(chunk_sz)
                if not chunk:
                    break
                if isinstance(chunk, str):
                    chunk = chunk.encode()
                total += len(chunk)
                if total > limit:
                    raise HTTPException(
                        status_code=413, detail=f"File exceeds {MAX_UPLOAD_MB} MB."
                    )
                tmp_fp.write(chunk)
        await storage.put_from_path(book_key, tmp_path)
    finally:
        tmp_path.unlink(missing_ok=True)

    book_path = storage.local_path(book_key)

    # Parse chapters (CPU-bound)
    loop = asyncio.get_running_loop()
    try:
        chapters = await loop.run_in_executor(_cpu_executor, _parse_book, book_path, ext)
    except Exception as exc:
        await storage.delete(book_key)
        log.error("Parse failed for %s: %s", file.filename, exc)
        raise HTTPException(status_code=422, detail=str(exc))

    # Use 'pending' (not 'queued') until the user selects chapters and POSTs
    # to /synthesize. Workers only claim jobs with status='queued', so a
    # pending job sits safely in the DB until the user is ready.
    job = Job(
        id=job_id,
        status="pending",
        voice=DEFAULT_VOICE,
        chapters_json=serialize_chapters(chapters),
        book_key=book_key,
        client_ip=_client_ip(request),
    )
    await repo.create(job)

    log.info("Uploaded %s → job %s (%d chapters)", file.filename, job_id, len(chapters))
    return {"job_id": job_id, "chapters": [{"number": c.number, "title": c.title} for c in chapters]}


def _parse_book(path: Path, ext: str):
    from parsers.epub_parser import EPUBParser
    from parsers.pdf_parser import PDFParser
    parser = PDFParser(path) if ext == ".pdf" else EPUBParser(path)
    chapters = parser.parse()
    if not chapters:
        raise ValueError("No readable chapters found in the uploaded file.")
    return chapters


# ─── Job state (for page-reload restore) ──────────────────────────────────────

@app.get("/job/{job_id}")
async def get_job_state(job_id: str):
    """Return current job state so the frontend can restore itself after a reload."""
    job          = await _get_job(job_id)
    chapters_raw = json.loads(job.chapters_json)
    audio_keys   = json.loads(job.audio_keys)
    preview_keys = json.loads(job.preview_keys)

    # Chapters with fully assembled audio
    audio_ready = [
        {"number": int(k), "title": next(
            (c["title"] for c in chapters_raw if str(c["number"]) == k), ""
        )}
        for k, v in audio_keys.items()
        if await storage.exists(v)
    ]
    # Chapters with only preview audio (not yet fully assembled)
    preview_ready = [
        {"number": int(k), "title": next(
            (c["title"] for c in chapters_raw if str(c["number"]) == k), ""
        )}
        for k, v in preview_keys.items()
        if k not in audio_keys and await storage.exists(v)
    ]

    return {
        "id":            job.id,
        "status":        job.status,
        "progress":      job.progress,
        "message":       job.message,
        "error":         job.error,
        "voice":         job.voice,
        "chapters":      [{"number": c["number"], "title": c["title"]} for c in chapters_raw],
        "selected":      job.selected,
        "audio_ready":   audio_ready,
        "preview_ready": preview_ready,
    }


# ─── Synthesize ───────────────────────────────────────────────────────────────

class SynthesizeRequest(BaseModel):
    chapters: list[int]
    voice: str = DEFAULT_VOICE


@app.post("/synthesize/{job_id}")
async def start_synthesis(job_id: str, req: SynthesizeRequest, request: Request):
    _check_auth(request)
    ip  = _client_ip(request)
    job = await _get_job(job_id)

    if job.status not in ("pending", "queued", "error", "done"):
        raise HTTPException(status_code=409, detail=f"Job is already '{job.status}'.")

    if not req.chapters:
        raise HTTPException(status_code=400, detail="Select at least one chapter.")

    if await repo.count_active_for_ip(ip) >= MAX_JOBS_PER_IP:
        raise HTTPException(status_code=429, detail="Too many active jobs. Please wait.")

    if await repo.count_active_global() >= MAX_GLOBAL_JOBS:
        raise HTTPException(status_code=503, detail="Server busy — try again in a minute.")

    await repo.update(
        job_id,
        status="queued",
        voice=req.voice,
        selected_json=json.dumps(req.chapters),
        audio_keys="{}",
        preview_keys="{}",
        progress=0.0,
        message="Queued for synthesis…",
        error=None,
        client_ip=ip,
    )
    log.info("Queued job %s (%d chapters, voice=%s)", job_id, len(req.chapters), req.voice)
    return {"status": "queued", "job_id": job_id}


# ─── Progress (SSE, DB-polled) ────────────────────────────────────────────────

def _sse(data: dict) -> str:
    return f"data: {json.dumps(data)}\n\n"


@app.get("/progress/{job_id}")
async def progress_stream(job_id: str):
    async def gen():
        seen_audio   = set()
        seen_preview = set()

        while True:
            job = await repo.get(job_id)
            if job is None:
                yield _sse({"status": "error", "error": "Job not found."})
                return

            audio_keys   = json.loads(job.audio_keys)
            preview_keys = json.loads(job.preview_keys)
            chapters_raw = json.loads(job.chapters_json)
            ch_lookup    = {str(c["number"]): c["title"] for c in chapters_raw}

            # Emit preview_ready for any new preview that arrived
            for ch_num, p_key in preview_keys.items():
                if ch_num not in seen_preview and await storage.exists(p_key):
                    seen_preview.add(ch_num)
                    yield _sse({
                        "status":        "preview_ready",
                        "chapter":       {"number": int(ch_num), "title": ch_lookup.get(ch_num, "")},
                        "progress":      job.progress,
                    })

            # Emit chapter_ready for any newly completed chapters
            for ch_num, a_key in audio_keys.items():
                if ch_num not in seen_audio and await storage.exists(a_key):
                    seen_audio.add(ch_num)
                    yield _sse({
                        "status":   "chapter_ready",
                        "chapter":  {"number": int(ch_num), "title": ch_lookup.get(ch_num, "")},
                        "progress": job.progress,
                        "message":  job.message,
                    })

            # Regular progress heartbeat
            payload: dict = {"status": job.status, "progress": job.progress,
                             "message": job.message}
            if job.status == "done":
                payload["chapters"] = [
                    {"number": int(k), "title": ch_lookup.get(k, "")}
                    for k in audio_keys
                ]
            if job.error:
                payload["error"] = job.error
            yield _sse(payload)

            if job.status in ("done", "error"):
                return

            await asyncio.sleep(1)

    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# ─── Audio serving ────────────────────────────────────────────────────────────

@app.get("/audio/{job_id}")
async def stream_audio(job_id: str, chapter: int | None = None):
    """Serve chapter audio with Range support. Falls back to preview if full isn't ready."""
    job  = await _get_job(job_id)
    path = await _resolve_audio_path(job, chapter, prefer_preview=False)
    return FileResponse(str(path), media_type="audio/mpeg")


@app.get("/download/{job_id}")
async def download_audio(job_id: str, chapter: int | None = None):
    job  = await _get_job(job_id)
    path = await _resolve_audio_path(job, chapter, prefer_preview=False)

    chapters_raw = json.loads(job.chapters_json)
    title = next(
        (c["title"] for c in chapters_raw if str(c["number"]) == str(chapter)),
        f"Chapter {chapter}" if chapter else "audiobook",
    )
    safe = _safe_filename(title)
    return FileResponse(
        str(path), media_type="audio/mpeg", filename=f"{safe}.mp3",
        headers={"Content-Disposition": f'attachment; filename="{safe}.mp3"'},
    )


async def _resolve_audio_path(job, chapter: int | None, prefer_preview: bool) -> Path:
    ch_num       = str(chapter) if chapter is not None else None
    audio_keys   = json.loads(job.audio_keys)
    preview_keys = json.loads(job.preview_keys)

    # When no chapter is specified, pick the first chapter that has ANY audio
    # (full or preview). This prevents a 404 when only a preview is available
    # for the first chapter while synthesis is still running.
    if ch_num is None:
        all_ready = sorted(set(audio_keys) | set(preview_keys), key=int)
        ch_num    = all_ready[0] if all_ready else None
    if ch_num is None:
        raise HTTPException(status_code=404, detail="Audio not ready yet.")

    if not prefer_preview:
        key = audio_keys.get(ch_num)
        if key and await storage.exists(key):
            return storage.local_path(key)

    # Fall back to preview if full chapter isn't ready
    p_key = preview_keys.get(ch_num)
    if p_key and await storage.exists(p_key):
        return storage.local_path(p_key)

    raise HTTPException(status_code=404, detail=f"Chapter {ch_num} not ready yet.")


# ─── Voices ───────────────────────────────────────────────────────────────────

_FEMALE_NAMES = {
    "Aria", "Jenny", "Sonia", "Natasha", "Clara", "Neerja",
    "Emma", "Michelle", "Elizabeth", "Ana", "Libby", "Maisie",
    "Emily", "Yan", "Leah",
}


@app.get("/voices")
async def get_voices():
    result = []
    for v in VOICES:
        parts   = v.split("-")
        locale  = "-".join(parts[:2]) if len(parts) >= 2 else "?"
        display = parts[-1].replace("Neural", "").replace("Multilingual", "")
        gender  = "Female" if any(n in v for n in _FEMALE_NAMES) else "Male"
        result.append({"name": v, "locale": locale, "gender": gender, "display": display})
    return result


# ─── Demo audio ───────────────────────────────────────────────────────────────

async def _synthesize_demo(text: str, voice: str, rate: str = "+0%") -> bytes:
    communicate = edge_tts.Communicate(text=text, voice=voice, rate=rate, pitch="+0Hz")
    data = b""
    async for chunk in communicate.stream():
        if chunk["type"] == "audio":
            data += chunk["data"]
    return data


@app.get("/demo/hero")
async def demo_hero():
    key = "demo/hero.mp3"
    if not await storage.exists(key):
        log.info("Generating hero demo audio…")
        try:
            data = await _synthesize_demo(_HERO_DEMO_TEXT, "en-GB-SoniaNeural", rate="-5%")
            await storage.put(key, data)
        except Exception as exc:
            log.error("Hero demo failed: %s", exc)
            raise HTTPException(status_code=503, detail="Demo audio temporarily unavailable.")
    return FileResponse(
        str(storage.local_path(key)), media_type="audio/mpeg",
        headers={"Cache-Control": "public, max-age=86400"},
    )


@app.get("/demo/voice/{voice_id}")
async def demo_voice(voice_id: str):
    if voice_id not in VOICES:
        raise HTTPException(status_code=404, detail=f"Voice '{voice_id}' not available.")
    key = f"demo/voice_{voice_id}.mp3"
    if not await storage.exists(key):
        log.info("Generating voice demo for %s…", voice_id)
        text = DEMO_QUOTES.get(voice_id, "Hello, I am reading your book with natural prosody.")
        try:
            data = await _synthesize_demo(text, voice_id)
            await storage.put(key, data)
        except Exception as exc:
            log.error("Voice demo failed for %s: %s", voice_id, exc)
            raise HTTPException(status_code=503, detail="Demo audio temporarily unavailable.")
    return FileResponse(
        str(storage.local_path(key)), media_type="audio/mpeg",
        headers={"Cache-Control": "public, max-age=86400"},
    )


# ─── Helpers ──────────────────────────────────────────────────────────────────

async def _get_job(job_id: str):
    job = await repo.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail=f"Job '{job_id}' not found.")
    return job
