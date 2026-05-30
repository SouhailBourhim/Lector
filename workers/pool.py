"""
workers/pool.py — Bounded async worker pool for audio synthesis.

N workers (default 4) poll the DB for queued jobs and run the full pipeline:
analyze → preview clip → synthesize → assemble → store.

On server restart, JobRepo._requeue_interrupted moves in-flight jobs back to
'queued' and workers pick them up automatically. Already-completed chapters
(their key exists in storage) are skipped, so restarts resume mid-book.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import tempfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from storage.backend import LocalStorage
    from storage.db import Job, JobRepo

log = logging.getLogger("lector.workers")

# Leading segments synthesized immediately as a low-latency "preview" clip.
# At ~3 s/segment, 5 segments ≈ 10–20 s of audible audio.
PREVIEW_SEGMENTS = 5


def chapter_cache_key(chapter, voice: str) -> str:
    """Stable cache key: SHA256(plain_text + '|' + voice)."""
    return hashlib.sha256((chapter.plain_text() + "|" + voice).encode()).hexdigest()


class WorkerPool:
    def __init__(
        self,
        n_workers: int,
        repo: "JobRepo",
        storage: "LocalStorage",
        cpu_executor: ThreadPoolExecutor,
    ) -> None:
        self._n          = n_workers
        self.repo        = repo
        self.storage     = storage
        self._executor   = cpu_executor
        self._tasks: list[asyncio.Task] = []
        self._stopping   = False
        self._active     = 0

        # In-process counters for /metrics (reset on restart)
        self.counters: dict[str, int] = {
            "jobs_done":      0,
            "jobs_error":     0,
            "cache_hits":     0,
            "tts_segments":   0,
        }

    @property
    def active_count(self) -> int:
        return self._active

    # ── lifecycle ─────────────────────────────────────────────────────────────

    async def start(self) -> None:
        self._stopping = False
        self._tasks = [
            asyncio.create_task(self._worker_loop(i), name=f"lector-worker-{i}")
            for i in range(self._n)
        ]
        log.info("WorkerPool started (%d workers)", self._n)

    async def stop(self) -> None:
        self._stopping = True
        for t in self._tasks:
            t.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        log.info("WorkerPool stopped")

    # ── worker loop ───────────────────────────────────────────────────────────

    async def _worker_loop(self, worker_id: int) -> None:
        while not self._stopping:
            try:
                job = await self.repo.claim_next()
                if job is None:
                    await asyncio.sleep(1)
                    continue

                self._active += 1
                log.info("[w%d] claimed job %s", worker_id, job.id)
                try:
                    await self._run_job(job)
                    self.counters["jobs_done"] += 1
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    log.exception("[w%d] job %s failed", worker_id, job.id)
                    await self.repo.update(job.id, status="error", error=str(exc))
                    self.counters["jobs_error"] += 1
                finally:
                    self._active -= 1

            except asyncio.CancelledError:
                return
            except Exception:
                log.exception("[w%d] unexpected loop error", worker_id)
                await asyncio.sleep(2)

    # ── job execution ─────────────────────────────────────────────────────────

    async def _run_job(self, job: "Job") -> None:  # noqa: C901 — long but linear
        from analyzer.text_analyzer import TextAnalyzer
        from assembler.audio import assemble_chapter, export_chapter
        from storage.db import deserialize_chapters
        from synthesizer.tts_engine import TTSEngine

        loop       = asyncio.get_running_loop()
        chapters   = deserialize_chapters(job.chapters_json)
        selected   = set(job.selected)
        to_process = [ch for ch in chapters if ch.number in selected]
        total      = len(to_process)

        if total == 0:
            await self.repo.update(job.id, status="error", error="No matching chapters found.")
            return

        analyzer     = TextAnalyzer()
        engine       = TTSEngine(voice=job.voice)
        audio_keys   = json.loads(job.audio_keys)
        preview_keys = json.loads(job.preview_keys)

        for ch_idx, chapter in enumerate(to_process):
            ch_base = ch_idx / total
            ch_span = 1.0   / total
            ch_num  = str(chapter.number)

            # ── skip already-completed chapters (restart recovery) ────────────
            if ch_num in audio_keys and await self.storage.exists(audio_keys[ch_num]):
                log.info("Skipping already-done chapter %s for job %s", ch_num, job.id)
                continue

            await self._update(job.id, "analyzing", ch_base,
                               f"Analyzing chapter {chapter.number} — {chapter.title}")

            # ── cache check ───────────────────────────────────────────────────
            cache_key  = chapter_cache_key(chapter, job.voice)
            cache_path = f"cache/{cache_key}.mp3"

            if await self.storage.exists(cache_path):
                log.info("Cache hit: ch %s, job %s", ch_num, job.id)
                self.counters["cache_hits"] += 1
                a_key            = f"jobs/{job.id}/{ch_num}.mp3"
                audio_keys[ch_num] = a_key
                data             = await self.storage.get(cache_path)
                await self.storage.put(a_key, data)
                await self.repo.update(
                    job.id,
                    status="synthesizing",
                    progress=ch_base + ch_span,
                    audio_keys=json.dumps(audio_keys),
                    message=f"Chapter {chapter.number} ready (cached)",
                )
                continue

            # ── spaCy analysis ────────────────────────────────────────────────
            segments = await loop.run_in_executor(
                self._executor, analyzer.analyze_chapter, chapter
            )

            # ── preview clip ──────────────────────────────────────────────────
            if ch_num not in preview_keys:
                await self._update(job.id, "synthesizing", ch_base + ch_span * 0.05,
                                   f"Building preview for chapter {chapter.number}…")
                preview_segs  = segments[:PREVIEW_SEGMENTS]
                preview_clips = await engine.synthesize_chapter_async(preview_segs)
                self.counters["tts_segments"] += len(preview_segs)

                p_key = await self._export_and_store(
                    preview_segs, preview_clips, f"jobs/{job.id}/{ch_num}_preview.mp3",
                    loop, assemble_chapter, export_chapter,
                )
                preview_keys[ch_num] = p_key
                await self.repo.update(job.id, preview_keys=json.dumps(preview_keys),
                                       message=f"Preview ready — chapter {chapter.number}")
                log.info("Preview ready: ch %s, job %s", ch_num, job.id)

            # ── full synthesis ────────────────────────────────────────────────
            await self._update(job.id, "synthesizing", ch_base + ch_span * 0.1,
                               f"Synthesizing chapter {chapter.number} ({len(segments)} segs)…")

            audio_clips = await engine.synthesize_chapter_async(segments)
            self.counters["tts_segments"] += len(segments)

            # ── assemble + store ──────────────────────────────────────────────
            await self._update(job.id, "assembling", ch_base + ch_span * 0.87,
                               f"Assembling chapter {chapter.number}…")

            a_key = await self._export_and_store(
                segments, audio_clips, f"jobs/{job.id}/{ch_num}.mp3",
                loop, assemble_chapter, export_chapter,
            )
            audio_keys[ch_num] = a_key

            # Write to chapter cache for future requests
            data = await self.storage.get(a_key)
            await self.storage.put(cache_path, data)

            await self.repo.update(
                job.id,
                status="synthesizing",
                progress=ch_base + ch_span,
                audio_keys=json.dumps(audio_keys),
                preview_keys=json.dumps(preview_keys),
                message=f"Chapter {chapter.number} ready",
            )
            log.info("Chapter %s done for job %s", ch_num, job.id)

        # ── finalise ──────────────────────────────────────────────────────────
        await self.repo.update(
            job.id,
            status="done",
            progress=1.0,
            message="Your audiobook is ready.",
            audio_keys=json.dumps(audio_keys),
            preview_keys=json.dumps(preview_keys),
        )
        log.info("Job %s complete (%d chapters)", job.id, total)

    # ── helpers ───────────────────────────────────────────────────────────────

    async def _update(self, job_id: str, status: str, progress: float, message: str) -> None:
        await self.repo.update(job_id, status=status, progress=progress, message=message)

    async def _export_and_store(
        self, segments, clips, key: str, loop, assemble_fn, export_fn
    ) -> str:
        """Assemble clips, export to temp MP3, store under key, return key."""
        audio = await loop.run_in_executor(self._executor, assemble_fn, segments, clips)
        with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False) as tmp:
            tmp_path = Path(tmp.name)
        try:
            await loop.run_in_executor(self._executor, export_fn, audio, tmp_path)
            await self.storage.put_from_path(key, tmp_path)
        finally:
            tmp_path.unlink(missing_ok=True)
        return key
