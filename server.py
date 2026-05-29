"""
Lector — FastAPI Web Server
Run: uvicorn server:app --reload --port 8000
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import shutil
import sys
import tempfile
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

import aiofiles
import aiofiles.os
from fastapi import FastAPI, File, HTTPException, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel

# ─── Path bootstrap ───────────────────────────────────────────────────────────
BASE_DIR = Path(__file__).parent
sys.path.insert(0, str(BASE_DIR))

from config import DEFAULT_VOICE, VOICES  # noqa: E402

# ─── Config from env ──────────────────────────────────────────────────────────
PORT          = int(os.getenv("PORT", "8000"))
MAX_UPLOAD_MB = int(os.getenv("MAX_UPLOAD_MB", "50"))
CACHE_DIR     = Path(os.getenv("CACHE_DIR", "/tmp/lector_cache"))
MAX_JOBS_PER_IP = 3

# ─── Logging ──────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger("lector")

# ─── App setup ────────────────────────────────────────────────────────────────
app = FastAPI(title="Lector", version="1.0.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)
app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))

# ─── Job store ────────────────────────────────────────────────────────────────
UPLOAD_DIR: Path | None = None
jobs: dict[str, "Job"] = {}


@dataclass
class Job:
    id: str
    status: str = "pending"   # pending|parsing|analyzing|synthesizing|assembling|done|error
    progress: float = 0.0
    message: str = ""
    chapters: list = field(default_factory=list)
    selected_chapters: list = field(default_factory=list)
    voice: str = DEFAULT_VOICE
    audio_paths: dict = field(default_factory=dict)   # chapter_num → Path
    error: str | None = None
    book_path: Path | None = None
    created_at: float = field(default_factory=time.time)
    client_ip: str = ""
    task: asyncio.Task | None = None
    # SSE queue: each item is a dict payload; None signals stream end
    _sse_queue: asyncio.Queue = field(default_factory=lambda: asyncio.Queue(maxsize=256))


def _get_job(job_id: str) -> Job:
    job = jobs.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail=f"Job '{job_id}' not found.")
    return job


def _client_ip(request: Request) -> str:
    forwarded = request.headers.get("x-forwarded-for", "")
    return forwarded.split(",")[0].strip() if forwarded else (request.client.host if request.client else "unknown")


def _active_jobs_for_ip(ip: str) -> int:
    return sum(
        1 for j in jobs.values()
        if j.client_ip == ip and j.status not in ("done", "error")
    )


# ─── Cache helpers ────────────────────────────────────────────────────────────
CACHE_TTL = 7 * 24 * 3600  # 7 days


def _chapter_cache_key(chapter, voice: str) -> str:
    return hashlib.sha256((chapter.plain_text() + "|" + voice).encode()).hexdigest()


def _get_cached(key: str) -> Path | None:
    path = CACHE_DIR / f"{key}.mp3"
    if path.exists() and (time.time() - path.stat().st_mtime) < CACHE_TTL:
        return path
    return None


def _save_to_cache(key: str, audio_path: Path) -> None:
    try:
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        dest = CACHE_DIR / f"{key}.mp3"
        shutil.copy2(audio_path, dest)
    except OSError as e:
        log.warning("Cache write failed: %s", e)


# ─── Startup / cleanup ────────────────────────────────────────────────────────
@app.on_event("startup")
async def startup():
    global UPLOAD_DIR
    UPLOAD_DIR = Path(tempfile.mkdtemp(prefix="lector_uploads_"))
    (UPLOAD_DIR / "audio").mkdir(parents=True, exist_ok=True)
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    asyncio.create_task(_cleanup_loop())
    log.info("Lector started — upload dir: %s  cache dir: %s", UPLOAD_DIR, CACHE_DIR)


async def _cleanup_loop():
    """Remove jobs + files older than 1 hour; runs every 30 min."""
    while True:
        await asyncio.sleep(30 * 60)
        now = time.time()
        stale = [jid for jid, j in list(jobs.items()) if now - j.created_at > 3600]
        for jid in stale:
            job = jobs.pop(jid, None)
            if not job:
                continue
            if job.task and not job.task.done():
                job.task.cancel()
            for path in job.audio_paths.values():
                try:
                    await aiofiles.os.remove(path)
                except OSError:
                    pass
            if job.book_path:
                try:
                    await aiofiles.os.remove(job.book_path)
                except OSError:
                    pass
            log.info("Cleaned up stale job %s", jid)


# ─── Routes ───────────────────────────────────────────────────────────────────

@app.get("/health")
async def health():
    return {"status": "ok", "version": "1.0.0"}


@app.get("/")
async def landing(request: Request):
    return templates.TemplateResponse(request=request, name="landing.html")


@app.get("/app")
async def index(request: Request):
    return templates.TemplateResponse(request=request, name="index.html")


# ── Upload ─────────────────────────────────────────────────────────────────────
@app.post("/upload")
async def upload_book(request: Request, file: UploadFile = File(...)):
    ip = _client_ip(request)

    # Size guard: check Content-Length header early
    content_length = request.headers.get("content-length")
    if content_length and int(content_length) > MAX_UPLOAD_MB * 1024 * 1024:
        raise HTTPException(status_code=413, detail=f"File exceeds {MAX_UPLOAD_MB} MB limit.")

    ext = Path(file.filename or "").suffix.lower()
    if ext not in (".pdf", ".epub"):
        raise HTTPException(status_code=400, detail="Only PDF and EPUB files are supported.")

    job_id    = str(uuid.uuid4())
    book_path = UPLOAD_DIR / f"{job_id}{ext}"

    # Stream to disk, enforce size limit
    total = 0
    limit = MAX_UPLOAD_MB * 1024 * 1024
    async with aiofiles.open(book_path, "wb") as f:
        while True:
            chunk = await file.read(64 * 1024)
            if not chunk:
                break
            total += len(chunk)
            if total > limit:
                await f.close()
                try:
                    await aiofiles.os.remove(book_path)
                except OSError:
                    pass
                raise HTTPException(status_code=413, detail=f"File exceeds {MAX_UPLOAD_MB} MB limit.")
            await f.write(chunk)

    log.info("Uploaded %s (%.1f MB) for job %s from %s", file.filename, total / 1e6, job_id, ip)

    job = Job(id=job_id, status="parsing", book_path=book_path, client_ip=ip)
    jobs[job_id] = job

    loop = asyncio.get_running_loop()
    try:
        if ext == ".pdf":
            from parsers.pdf_parser import PDFParser
            parser = PDFParser(book_path)
        else:
            from parsers.epub_parser import EPUBParser
            parser = EPUBParser(book_path)

        chapters = await loop.run_in_executor(None, parser.parse)
    except Exception as exc:
        job.status = "error"
        job.error  = str(exc)
        try:
            await aiofiles.os.remove(book_path)
        except OSError:
            pass
        log.error("Parse failed for job %s: %s", job_id, exc)
        raise HTTPException(status_code=422, detail=str(exc))

    if not chapters:
        raise HTTPException(status_code=422, detail="No readable chapters found in this file.")

    job.chapters = chapters
    job.status   = "pending"

    return {
        "job_id":   job_id,
        "chapters": [{"number": ch.number, "title": ch.title} for ch in chapters],
    }


# ── Synthesize ─────────────────────────────────────────────────────────────────
class SynthesizeRequest(BaseModel):
    chapters: list[int]
    voice: str = DEFAULT_VOICE


@app.post("/synthesize/{job_id}")
async def start_synthesis(job_id: str, req: SynthesizeRequest, request: Request):
    ip  = _client_ip(request)
    job = _get_job(job_id)

    if job.status not in ("pending", "error", "done"):
        raise HTTPException(status_code=409, detail=f"Job is already in '{job.status}' state.")

    if not req.chapters:
        raise HTTPException(status_code=400, detail="Select at least one chapter.")

    active = _active_jobs_for_ip(ip)
    if active >= MAX_JOBS_PER_IP:
        raise HTTPException(
            status_code=429,
            detail=f"Too many active jobs ({active}). Please wait for a current job to finish.",
        )

    job.selected_chapters = req.chapters
    job.voice             = req.voice
    job.status            = "analyzing"
    job.progress          = 0.0
    job.message           = "Starting…"
    job.error             = None
    job.audio_paths       = {}
    job._sse_queue        = asyncio.Queue(maxsize=256)

    task = asyncio.create_task(_run_synthesis(job_id))
    job.task = task
    log.info("Synthesis started for job %s (%d chapters, voice=%s)", job_id, len(req.chapters), req.voice)
    return {"status": "started", "job_id": job_id}


# ── Progress (SSE) ─────────────────────────────────────────────────────────────
@app.get("/progress/{job_id}")
async def progress_stream(job_id: str):
    _get_job(job_id)

    async def _gen():
        job = jobs.get(job_id)
        if not job:
            yield _sse_json({"status": "error", "progress": 0,
                             "message": "Job not found", "error": "Job not found"})
            return

        while True:
            try:
                payload = await asyncio.wait_for(job._sse_queue.get(), timeout=30.0)
            except asyncio.TimeoutError:
                # Heartbeat to keep connection alive
                yield ": heartbeat\n\n"
                continue

            if payload is None:
                return

            yield _sse_json(payload)

            if payload.get("status") in ("done", "error"):
                return

    return StreamingResponse(
        _gen(),
        media_type="text/event-stream",
        headers={
            "Cache-Control":     "no-cache",
            "X-Accel-Buffering": "no",
            "Connection":        "keep-alive",
        },
    )


def _sse_json(data: dict) -> str:
    return f"data: {json.dumps(data)}\n\n"


def _push(job: Job, payload: dict | None) -> None:
    """Non-blocking SSE push; drops oldest item if queue is full."""
    try:
        job._sse_queue.put_nowait(payload)
    except asyncio.QueueFull:
        try:
            job._sse_queue.get_nowait()
        except asyncio.QueueEmpty:
            pass
        try:
            job._sse_queue.put_nowait(payload)
        except asyncio.QueueFull:
            pass


# ── Audio streaming ────────────────────────────────────────────────────────────
@app.get("/audio/{job_id}")
async def stream_audio(job_id: str, chapter: int | None = None):
    job  = _get_job(job_id)
    path = _resolve_audio(job, chapter)

    async def _file_stream():
        async with aiofiles.open(path, "rb") as f:
            while True:
                chunk = await f.read(65536)
                if not chunk:
                    break
                yield chunk

    return StreamingResponse(
        _file_stream(),
        media_type="audio/mpeg",
        headers={"Accept-Ranges": "bytes"},
    )


# ── Download ──────────────────────────────────────────────────────────────────
@app.get("/download/{job_id}")
async def download_audio(job_id: str, chapter: int | None = None):
    job  = _get_job(job_id)
    path = _resolve_audio(job, chapter)

    safe_title = _safe_filename(
        next(
            (ch.title for ch in job.chapters if ch.number == chapter),
            f"Chapter {chapter}" if chapter else "audiobook",
        )
    )
    return FileResponse(
        path=str(path),
        media_type="audio/mpeg",
        filename=f"{safe_title}.mp3",
        headers={"Content-Disposition": f'attachment; filename="{safe_title}.mp3"'},
    )


def _resolve_audio(job: Job, chapter: int | None) -> Path:
    if job.status != "done" or not job.audio_paths:
        raise HTTPException(status_code=404, detail="Audio not ready yet.")
    chap = chapter if chapter is not None else sorted(job.audio_paths.keys())[0]
    if chap not in job.audio_paths:
        raise HTTPException(status_code=404, detail=f"Chapter {chap} not available.")
    return job.audio_paths[chap]


def _safe_filename(s: str) -> str:
    return "".join(c if c.isalnum() or c in " -_()" else "_" for c in s).strip("_ ")[:80]


# ── Voices ─────────────────────────────────────────────────────────────────────
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


# ─── Background synthesis task ────────────────────────────────────────────────
async def _run_synthesis(job_id: str) -> None:
    job  = jobs[job_id]
    loop = asyncio.get_running_loop()

    async def _do_synthesis():
        from analyzer.text_analyzer import TextAnalyzer
        from assembler.audio import assemble_chapter, export_chapter
        from synthesizer.tts_engine import TTSEngine

        selected_nums  = set(job.selected_chapters)
        selected       = [ch for ch in job.chapters if ch.number in selected_nums]
        total_chapters = len(selected)

        if total_chapters == 0:
            raise ValueError("No matching chapters found.")

        analyzer = TextAnalyzer()
        engine   = TTSEngine(voice=job.voice)

        for ch_idx, chapter in enumerate(selected):
            ch_base = ch_idx / total_chapters
            ch_span = 1.0   / total_chapters

            # Check cache
            cache_key    = _chapter_cache_key(chapter, job.voice)
            cached_path  = _get_cached(cache_key)

            if cached_path:
                log.info("Cache hit for chapter %d of job %s", chapter.number, job_id)
                audio_path = UPLOAD_DIR / "audio" / f"{job_id}_{chapter.number}.mp3"
                shutil.copy2(cached_path, audio_path)
                job.audio_paths[chapter.number] = audio_path
                job.progress = ch_base + ch_span
                job.message  = f"Loaded from cache — \"{chapter.title}\""
                _push(job, {
                    "status":   "chapter_ready",
                    "progress": round(job.progress, 4),
                    "message":  job.message,
                    "error":    None,
                    "chapter":  {
                        "number": chapter.number,
                        "title":  chapter.title,
                    },
                })
                continue

            # ── Analysis ──────────────────────────────────────────────────
            job.status   = "analyzing"
            job.message  = f"Analyzing \"{chapter.title}\"…"
            job.progress = ch_base + ch_span * 0.05
            _push(job, {"status": job.status, "progress": round(job.progress, 4),
                        "message": job.message, "error": None})

            segments = await loop.run_in_executor(None, analyzer.analyze_chapter, chapter)
            job.progress = ch_base + ch_span * 0.10

            # ── Synthesis ─────────────────────────────────────────────────
            job.status  = "synthesizing"
            total_segs  = len(segments)

            def _make_cb(base, span, total):
                def cb(completed, _total):
                    job.progress = base + span * (0.10 + 0.75 * completed / max(total, 1))
                    job.message  = (
                        f"Synthesizing segment {completed}/{total}"
                        f" — \"{chapter.title}\""
                    )
                    _push(job, {"status": job.status, "progress": round(job.progress, 4),
                                "message": job.message, "error": None})
                return cb

            progress_cb = _make_cb(ch_base, ch_span, total_segs)
            audio_clips = await engine.synthesize_chapter_async(segments, progress_cb)
            job.progress = ch_base + ch_span * 0.87

            # ── Assembly ──────────────────────────────────────────────────
            job.status  = "assembling"
            job.message = f"Assembling audio — \"{chapter.title}\""
            _push(job, {"status": job.status, "progress": round(job.progress, 4),
                        "message": job.message, "error": None})

            audio = await loop.run_in_executor(None, assemble_chapter, segments, audio_clips)
            job.progress = ch_base + ch_span * 0.95

            # ── Export ────────────────────────────────────────────────────
            audio_path = UPLOAD_DIR / "audio" / f"{job_id}_{chapter.number}.mp3"
            await loop.run_in_executor(None, export_chapter, audio, audio_path)

            _save_to_cache(cache_key, audio_path)
            job.audio_paths[chapter.number] = audio_path
            job.progress = ch_base + ch_span * 1.0

            # Emit chapter_ready so client can render audio card immediately
            _push(job, {
                "status":   "chapter_ready",
                "progress": round(job.progress, 4),
                "message":  f"Chapter ready — \"{chapter.title}\"",
                "error":    None,
                "chapter":  {
                    "number": chapter.number,
                    "title":  chapter.title,
                },
            })
            log.info("Chapter %d done for job %s", chapter.number, job_id)

        job.status   = "done"
        job.progress = 1.0
        job.message  = f"Done — {total_chapters} chapter{'s' if total_chapters != 1 else ''} ready"

        done_payload: dict = {
            "status":   "done",
            "progress": 1.0,
            "message":  job.message,
            "error":    None,
            "chapters": [
                {
                    "number": n,
                    "title":  next(
                        (ch.title for ch in job.chapters if ch.number == n),
                        f"Chapter {n}",
                    ),
                }
                for n in sorted(job.audio_paths.keys())
            ],
        }
        _push(job, done_payload)
        _push(job, None)  # signal SSE generator to close
        log.info("Job %s complete (%d chapters)", job_id, total_chapters)

    try:
        await asyncio.wait_for(_do_synthesis(), timeout=600)  # 10-minute hard limit
    except asyncio.TimeoutError:
        job.status  = "error"
        job.error   = "Synthesis timed out (10 minutes). Try fewer or shorter chapters."
        job.message = job.error
        _push(job, {"status": "error", "progress": job.progress,
                    "message": job.message, "error": job.error})
        _push(job, None)
        log.error("Job %s timed out", job_id)
    except asyncio.CancelledError:
        log.info("Job %s cancelled", job_id)
        raise
    except Exception as exc:
        job.status  = "error"
        job.error   = str(exc)
        job.message = f"Error: {exc}"
        _push(job, {"status": "error", "progress": job.progress,
                    "message": job.message, "error": job.error})
        _push(job, None)
        log.error("Job %s failed: %s", job_id, exc, exc_info=True)
