"""
Lector — FastAPI Web Server
Run: uvicorn server:app --reload --port 8000
"""

from __future__ import annotations

import asyncio
import json
import sys
import tempfile
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

import aiofiles
import aiofiles.os
from fastapi import FastAPI, File, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel

# ─── Path bootstrap ───────────────────────────────────────────────────────────
BASE_DIR = Path(__file__).parent
sys.path.insert(0, str(BASE_DIR))

from config import DEFAULT_VOICE, VOICES  # noqa: E402

# ─── App setup ────────────────────────────────────────────────────────────────
app = FastAPI(title="Lector", version="1.0.0")
app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))

# ─── Job store ────────────────────────────────────────────────────────────────
UPLOAD_DIR: Path | None = None
jobs: dict[str, "Job"] = {}


@dataclass
class Job:
    id: str
    status: str = "pending"          # pending|parsing|analyzing|synthesizing|assembling|done|error
    progress: float = 0.0            # 0.0–1.0
    message: str = ""
    chapters: list = field(default_factory=list)       # list[BookChapter]
    selected_chapters: list = field(default_factory=list)
    voice: str = DEFAULT_VOICE
    audio_paths: dict = field(default_factory=dict)    # chapter_num (int) → Path
    error: str | None = None
    book_path: Path | None = None
    created_at: float = field(default_factory=time.time)


def _get_job(job_id: str) -> Job:
    job = jobs.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail=f"Job '{job_id}' not found.")
    return job


# ─── Startup / cleanup ────────────────────────────────────────────────────────
@app.on_event("startup")
async def startup():
    global UPLOAD_DIR
    UPLOAD_DIR = Path(tempfile.mkdtemp(prefix="lector_uploads_"))
    (UPLOAD_DIR / "audio").mkdir(parents=True, exist_ok=True)
    asyncio.create_task(_cleanup_loop())


async def _cleanup_loop():
    """Remove jobs + files older than 1 hour, runs every 30 min."""
    while True:
        await asyncio.sleep(30 * 60)
        now = time.time()
        stale = [jid for jid, j in list(jobs.items()) if now - j.created_at > 3600]
        for jid in stale:
            job = jobs.pop(jid, None)
            if not job:
                continue
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


# ─── Routes ───────────────────────────────────────────────────────────────────

@app.get("/")
async def index(request: Request):
    return templates.TemplateResponse(request=request, name="index.html")


# ── Upload ─────────────────────────────────────────────────────────────────────
@app.post("/upload")
async def upload_book(file: UploadFile = File(...)):
    ext = Path(file.filename or "").suffix.lower()
    if ext not in (".pdf", ".epub"):
        raise HTTPException(status_code=400, detail="Only PDF and EPUB files are supported.")

    job_id    = str(uuid.uuid4())
    book_path = UPLOAD_DIR / f"{job_id}{ext}"

    # Stream file to disk
    async with aiofiles.open(book_path, "wb") as f:
        while True:
            chunk = await file.read(64 * 1024)
            if not chunk:
                break
            await f.write(chunk)

    # Parse in thread pool (CPU/IO bound)
    job = Job(id=job_id, status="parsing", book_path=book_path)
    jobs[job_id] = job

    loop = asyncio.get_event_loop()
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
        # Clean up uploaded file on parse error
        try:
            await aiofiles.os.remove(book_path)
        except OSError:
            pass
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
async def start_synthesis(job_id: str, req: SynthesizeRequest):
    job = _get_job(job_id)

    if job.status not in ("pending", "error", "done"):
        raise HTTPException(
            status_code=409,
            detail=f"Job is already in '{job.status}' state.",
        )

    if not req.chapters:
        raise HTTPException(status_code=400, detail="Select at least one chapter.")

    job.selected_chapters = req.chapters
    job.voice             = req.voice
    job.status            = "analyzing"
    job.progress          = 0.0
    job.message           = "Starting…"
    job.error             = None
    job.audio_paths       = {}

    asyncio.create_task(_run_synthesis(job_id))
    return {"status": "started", "job_id": job_id}


# ── Progress (SSE) ─────────────────────────────────────────────────────────────
@app.get("/progress/{job_id}")
async def progress_stream(job_id: str):
    _get_job(job_id)  # 404 early if not found

    async def _gen():
        last_progress = -1.0
        while True:
            job = jobs.get(job_id)
            if not job:
                yield _sse_json({"status": "error", "progress": 0,
                                 "message": "Job not found", "error": "Job not found"})
                return

            if job.progress != last_progress or job.status in ("done", "error"):
                payload: dict = {
                    "status":   job.status,
                    "progress": round(job.progress, 4),
                    "message":  job.message,
                    "error":    job.error,
                }
                # On completion, include chapter metadata for the result screen
                if job.status == "done":
                    payload["chapters"] = [
                        {
                            "number": n,
                            "title":  next(
                                (ch.title for ch in job.chapters if ch.number == n),
                                f"Chapter {n}",
                            ),
                        }
                        for n in sorted(job.audio_paths.keys())
                    ]
                yield _sse_json(payload)
                last_progress = job.progress

            if job.status in ("done", "error"):
                return

            await asyncio.sleep(0.25)

    return StreamingResponse(
        _gen(),
        media_type="text/event-stream",
        headers={
            "Cache-Control":      "no-cache",
            "X-Accel-Buffering":  "no",
            "Connection":         "keep-alive",
        },
    )


def _sse_json(data: dict) -> str:
    return f"data: {json.dumps(data)}\n\n"


# ── Audio streaming (for HTML5 player) ────────────────────────────────────────
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


# ── Download (attachment) ──────────────────────────────────────────────────────
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
    loop = asyncio.get_event_loop()

    try:
        from analyzer.text_analyzer import TextAnalyzer
        from assembler.audio import assemble_chapter, export_chapter
        from synthesizer.tts_engine import TTSEngine

        selected_nums = set(job.selected_chapters)
        selected      = [ch for ch in job.chapters if ch.number in selected_nums]
        total_chapters = len(selected)

        if total_chapters == 0:
            raise ValueError("No matching chapters found.")

        analyzer = TextAnalyzer()
        engine   = TTSEngine(voice=job.voice)

        for ch_idx, chapter in enumerate(selected):
            ch_base = ch_idx / total_chapters
            ch_span = 1.0   / total_chapters

            # ── Analysis ──────────────────────────────────────────────────
            job.status   = "analyzing"
            job.message  = f"Analyzing \"{chapter.title}\"…"
            job.progress = ch_base + ch_span * 0.05

            segments = await loop.run_in_executor(None, analyzer.analyze_chapter, chapter)

            job.progress = ch_base + ch_span * 0.10

            # ── Synthesis ─────────────────────────────────────────────────
            job.status = "synthesizing"
            total_segs = len(segments)

            def _make_cb(base, span, total):
                def cb(completed, _total):
                    job.progress = base + span * (0.10 + 0.75 * completed / max(total, 1))
                    job.message  = (
                        f"Synthesizing segment {completed}/{total}"
                        f" — \"{chapter.title}\""
                    )
                return cb

            progress_cb = _make_cb(ch_base, ch_span, total_segs)
            audio_clips = await engine.synthesize_chapter_async(segments, progress_cb)

            job.progress = ch_base + ch_span * 0.87

            # ── Assembly ──────────────────────────────────────────────────
            job.status  = "assembling"
            job.message = f"Assembling audio — \"{chapter.title}\""

            audio = await loop.run_in_executor(
                None, assemble_chapter, segments, audio_clips
            )

            job.progress = ch_base + ch_span * 0.95

            # ── Export ────────────────────────────────────────────────────
            audio_path = UPLOAD_DIR / "audio" / f"{job_id}_{chapter.number}.mp3"
            await loop.run_in_executor(None, export_chapter, audio, audio_path)

            job.audio_paths[chapter.number] = audio_path
            job.progress = ch_base + ch_span * 1.0

        job.status   = "done"
        job.progress = 1.0
        job.message  = f"Done — {total_chapters} chapter{'s' if total_chapters != 1 else ''} ready"

    except Exception as exc:
        job.status  = "error"
        job.error   = str(exc)
        job.message = f"Error: {exc}"
